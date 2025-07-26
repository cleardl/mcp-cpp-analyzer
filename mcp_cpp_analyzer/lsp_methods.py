import asyncio
import logging
import traceback
from pathlib import Path
from typing import List, Optional, Dict, Any

from .clangd_client import ClangdClient, path_to_uri, uri_to_path
from .utils import get_file_language_id
from .config import get_global_config

logger = logging.getLogger(__name__)


class DocumentManager:
    """Manages document state with clangd"""
    
    def __init__(self, lsp_client: ClangdClient):
        self.lsp_client = lsp_client
        self.open_documents: Dict[Path, dict] = {}
        
    async def ensure_document_open(self, file_path: Path) -> bool:
        """Ensure document is open in clangd"""
        
        file_path = file_path.resolve()
        logger.debug(f"Using file path: {file_path}")
        
        if file_path in self.open_documents:
            logger.debug(f"Document already open: {file_path}")
            return False
            
        try:
            logger.debug(f"Reading file content: {file_path}")
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            logger.debug(f"UTF-8 decode failed, trying latin-1: {file_path}")
            with open(file_path, 'r', encoding='latin-1') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read file {file_path}: {e}")
            raise
                
        language_id = get_file_language_id(file_path)

        uri = path_to_uri(file_path)
        logger.debug(f"Generated URI: {uri}")
        logger.debug(f"Language ID: {language_id}")
        
        try:
            await self.lsp_client.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": content
                }
            })
            logger.debug(f"didOpen notification sent successfully for {file_path}")
        except Exception as e:
            logger.error(f"Failed to send didOpen notification: {type(e).__name__}: {e}")
            raise
        
        self.open_documents[file_path] = {
            "uri": path_to_uri(file_path),
            "language_id": language_id,
            "version": 1
        }
        return True
    
    async def close_all_documents(self):
        for file_path, doc_info in self.open_documents.items():
            try:
                await self.lsp_client.notify("textDocument/didClose", {
                    "textDocument": {
                        "uri": doc_info["uri"]
                    }
                })
            except Exception as e:
                logger.warning(f"Error closing document {file_path}: {e}")
                
        self.open_documents.clear()
        logger.debug("Closed all documents")


class LSPMethods:
    def __init__(self, lsp_client: ClangdClient):
        self.lsp_client = lsp_client
        self.document_manager = DocumentManager(lsp_client)
        
    async def get_definition(self, file: str, line: int, column: int) -> Optional[List[Dict[str, Any]]]:
        """Get symbol definition"""
        
        file_path = Path(file).resolve()

        await self.document_manager.ensure_document_open(file_path)
        
        try:
            config = get_global_config()
            logger.debug(f"About to send textDocument/definition request")
            result = await self.lsp_client.request("textDocument/definition", {
                "textDocument": {"uri": path_to_uri(file_path)},
                "position": {"line": line - 1, "character": column - 1}
            }, timeout=config.lsp_definition_timeout)
        except Exception as e:
            logger.error(f"Definition request failed: {type(e).__name__}: {e}")
            logger.error(f"Definition request traceback: {traceback.format_exc()}")
            raise RuntimeError(f"Definition request failed: {e}")
        
        if not result:
            return None
            
        locations = result if isinstance(result, list) else [result]
        
        definitions = []
        for location in locations:
            if "uri" in location and "range" in location:
                uri = location["uri"]
                range_info = location["range"]
                start = range_info["start"]
                
                try:
                    file_path_def = uri_to_path(uri)
                    definitions.append({
                        "file": str(file_path_def),
                        "line": start["line"] + 1,
                        "column": start["character"] + 1,
                        "uri": uri,
                        "range": range_info
                    })
                except Exception as e:
                    logger.warning(f"Error processing definition location: {e}")
                    continue
        
        return definitions if definitions else None
    
    async def get_hover(self, file: str, line: int, column: int) -> Optional[Dict[str, Any]]:
        """Get hover information"""
        
        file_path = Path(file).resolve()
        
        await self.document_manager.ensure_document_open(file_path)
        
        try:
            config = get_global_config()
            result = await self.lsp_client.request("textDocument/hover", {
                "textDocument": {"uri": path_to_uri(file_path)},
                "position": {"line": line - 1, "character": column - 1}
            }, timeout=config.lsp_hover_timeout)
        except Exception as e:
            logger.error(f"Hover request failed: {type(e).__name__}: {e}")
            logger.error(f"Hover request traceback: {traceback.format_exc()}")
            raise RuntimeError(f"Hover request failed: {e}")
        
        if not result:
            return None
        
        contents = result.get("contents", {})
        range_info = result.get("range")
        
        response = {
            "contents": contents,
            "file": file,
            "line": line,
            "column": column
        }
        
        if range_info:
            response["range"] = range_info
        
        return response
    
    async def get_references(self, file: str, line: int, column: int, 
                           include_declaration: bool = True) -> Optional[List[Dict[str, Any]]]:
        """Find all references to a symbol"""
        
        file_path = Path(file).resolve()
        
        await self.document_manager.ensure_document_open(file_path)
        
        try:
            config = get_global_config()
            result = await self.lsp_client.request("textDocument/references", {
                "textDocument": {"uri": path_to_uri(file_path)},
                "position": {"line": line - 1, "character": column - 1},
                "context": {"includeDeclaration": include_declaration}
            }, timeout=config.lsp_references_timeout)
        except Exception as e:
            logger.error(f"References request failed: {type(e).__name__}: {e}")
            logger.error(f"References request traceback: {traceback.format_exc()}")
            raise RuntimeError(f"References request failed: {e}")
        
        if not result:
            return None
        
        references = []
        for location in result:
            if "uri" in location and "range" in location:
                uri = location["uri"]
                range_info = location["range"]
                start = range_info["start"]
                
                try:
                    file_path_ref = uri_to_path(uri)
                    references.append({
                        "file": str(file_path_ref),
                        "line": start["line"] + 1,
                        "column": start["character"] + 1,
                        "uri": uri,
                        "range": range_info
                    })
                except Exception as e:
                    logger.warning(f"Error processing reference location: {e}")
                    continue
        
        return references if references else None
    
    async def search_symbols(self, query: str) -> Optional[List[Dict[str, Any]]]:
        """Search for symbols in the workspace"""
        
        try:
            result = await self.lsp_client.request("workspace/symbol", {
                "query": query
            })
        except Exception as e:
            logger.error(f"Symbol search request failed: {type(e).__name__}: {e}")
            logger.error(f"Symbol search request traceback: {traceback.format_exc()}")
            raise RuntimeError(f"Symbol search request failed: {e}")
        
        if not result:
            return None
        
        symbols = []
        for symbol in result:
            try:
                location = symbol.get("location", {})
                uri = location.get("uri", "")
                
                if uri.startswith("file://"):
                    file_path = uri_to_path(uri)
                    range_info = location.get("range", {})
                    start = range_info.get("start", {})
                    
                    symbols.append({
                        "name": symbol.get("name", ""),
                        "kind": symbol.get("kind", 0),
                        "kind_name": self._symbol_kind_to_name(symbol.get("kind", 0)),
                        "container_name": symbol.get("containerName", ""),
                        "file": str(file_path),
                        "line": start.get("line", 0) + 1,
                        "column": start.get("character", 0) + 1,
                        "location": location
                    })
            except Exception as e:
                logger.warning(f"Error processing symbol: {e}")
                continue
        
        return symbols if symbols else None
    
    def _symbol_kind_to_name(self, kind: int) -> str:
        """Convert LSP SymbolKind enum to human-readable name"""
        kind_names = {
            1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
            6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
            11: "Interface", 12: "Function", 13: "Variable", 14: "Constant", 15: "String",
            16: "Number", 17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
            21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event", 25: "Operator",
            26: "TypeParameter"
        }
        return kind_names.get(kind, f"Unknown({kind})") 