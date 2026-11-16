"""One authenticated local backend and one clangd per workspace/build."""
import asyncio
from contextlib import ExitStack
import hmac
import logging
import os
from pathlib import Path
import secrets
import time

from fastmcp.server.context import reset_transport, set_transport

from .config import Config
from .query_service import QueryService
from .server import create_server
from .shared_protocol import MAX_FRAME, PROTOCOL, decode, encode, proof
from .shared_runtime import atomic_json, read_json
from .transport import socket_transport
from .windows_runtime import delete_task, process_created, workspace_lock

logger = logging.getLogger(__name__)


class SharedBackend:
    def __init__(self, profile):
        self.profile = profile
        self.directory = Path(profile["profile_path"]).parent
        self.instance_id = secrets.token_hex(16)
        self.token = secrets.token_hex(32)
        self.clients = set()
        self.tasks = set()
        self.last_detach = time.monotonic()
        self.stop = asyncio.Event()
        config = Config(workspace_root=profile["workspace"], compile_db_dir=profile["compile_db"],
                        clangd_path=profile["clangd"], tool_mode=profile["tool_mode"])
        self.service = QueryService(config)
        self.service.runtime_status = self.status
        self.mcp = create_server(config, service_factory=lambda _: self.service)
        self.listener = None

    def status(self):
        return {"pid": os.getpid(), "instance_id": self.instance_id, "clients": len(self.clients),
                "idle_timeout_sec": self.profile["idle_seconds"], "runtime_directory": str(self.directory)}

    async def authenticate(self, reader, writer):
        nonce = secrets.token_hex(32)
        writer.write(encode({"protocol": PROTOCOL, "nonce": nonce, "instance_id": self.instance_id}))
        await writer.drain()
        frame = await asyncio.wait_for(reader.readline(), 5)
        if len(frame) > 4096:
            raise ValueError("Authentication frame exceeds limit")
        value = decode(frame)
        expected = proof(self.token, "client", nonce, value["nonce"])
        if not hmac.compare_digest(value.get("proof", ""), expected):
            raise PermissionError("Shared-backend authentication failed")
        self.clients.add(writer)
        writer.write(encode({"proof": proof(self.token, "server", nonce, value["nonce"])}))
        await writer.drain()

    async def client(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        transport_token = set_transport("stdio")
        try:
            await self.authenticate(reader, writer)
            async with socket_transport(reader, writer) as (incoming, outgoing):
                low_level = self.mcp._mcp_server
                await low_level.run(incoming, outgoing, low_level.create_initialization_options())
        except (ConnectionError, PermissionError, ValueError, asyncio.TimeoutError) as error:
            logger.warning("Client connection rejected/closed: %s", error)
        finally:
            reset_transport(transport_token)
            connected = writer in self.clients
            self.clients.discard(writer)
            if connected:
                self.last_detach = time.monotonic()
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.tasks.discard(task)

    async def run(self):
        with workspace_lock("Local\\McpCppAnalyzerOwner-" + self.profile["key"], timeout=0), ExitStack() as shutdown:
            async with self.mcp._lifespan_manager():
                self.listener = await asyncio.start_server(self.client, "127.0.0.1", 0, limit=MAX_FRAME)
                endpoint = {"key": self.profile["key"], "launch_id": self.profile["launch_id"],
                            "pid": os.getpid(), "created": process_created(),
                            "instance_id": self.instance_id, "token": self.token,
                            "port": self.listener.sockets[0].getsockname()[1]}
                self.last_detach = time.monotonic()
                atomic_json(self.directory / "instance.json", endpoint)
                try:
                    while not self.stop.is_set():
                        await asyncio.sleep(0.2)
                        if self.clients or time.monotonic() - self.last_detach < self.profile["idle_seconds"]:
                            continue
                        try:
                            shutdown.enter_context(workspace_lock(self.profile["mutex"], timeout=0))
                        except TimeoutError:
                            continue
                        break
                finally:
                    self.listener.close()
                    await self.listener.wait_closed()
                    for task in tuple(self.tasks):
                        task.cancel()
                    if self.tasks:
                        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            published = read_json(self.directory / "instance.json")
            if published is not None and published["instance_id"] == self.instance_id:
                (self.directory / "instance.json").unlink()
            delete_task(self.profile["task_name"])
