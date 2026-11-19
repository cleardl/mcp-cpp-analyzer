"""A stdlib-only, duplex MCP relay with explicit failure on interrupted queries."""
import errno
import os
import queue
import secrets
import socket
import sys
import threading

from .shared_protocol import MAX_FRAME, decode, encode, read_frame
from .shared_runtime import ensure_connection


class Proxy:
    def __init__(self, profile, protocol):
        self.profile, self.protocol = profile, protocol
        self.events = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        self.connection = self.stream = self.reader_thread = None
        self.reader_stop = None
        self.generation = 0
        self.pending = set()
        self.server_requests = {}
        self.initialize = self.initialize_result = None
        self.initialized = False
        self.modern = False

    def emit(self, value):
        self.protocol.buffer.write(encode(value))
        self.protocol.buffer.flush()

    def post(self, event, stop):
        while not self.closed.is_set() and not stop.is_set():
            try:
                self.events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def stdin(self):
        buffer = bytearray()
        try:
            while not self.closed.is_set():
                chunk = os.read(0, 65536)
                if not chunk:
                    if buffer:
                        raise ValueError("stdin ended during a JSON-RPC frame")
                    self.post(("stdin_eof", None, None), self.closed)
                    return
                buffer.extend(chunk)
                while b"\n" in buffer:
                    frame, _, remaining = buffer.partition(b"\n")
                    buffer = bytearray(remaining)
                    if len(frame) > MAX_FRAME:
                        raise ValueError("stdin frame exceeds 16 MiB")
                    self.post(("stdin", None, frame + b"\n"), self.closed)
                if len(buffer) > MAX_FRAME:
                    raise ValueError("stdin frame exceeds 16 MiB")
        except (OSError, ValueError) as error:
            self.post(("stdin_error", None, error), self.closed)

    def read_backend(self, generation, stream, stop):
        try:
            while not stop.is_set():
                frame = read_frame(stream)
                if not frame:
                    break
                self.post(("backend", generation, frame), stop)
        except (OSError, ValueError, ConnectionError) as error:
            self.post(("backend_error", generation, error), stop)
        finally:
            stream.close()
            self.post(("backend_eof", generation, None), stop)

    def disconnect(self):
        if self.connection is None:
            return
        self.reader_stop.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            if error.errno not in (errno.ENOTCONN, errno.EBADF, errno.ECONNRESET) and getattr(error, "winerror", None) not in (10038, 10057):
                raise
        self.connection.close()
        if self.reader_thread is not None:
            self.reader_thread.join(2)
            if self.reader_thread.is_alive():
                raise RuntimeError("Backend reader did not stop after closing its socket")
        else:
            self.stream.close()
        self.connection = self.stream = self.reader_thread = None

    def attach(self, *, recover=False):
        _, connection, stream = ensure_connection(self.profile)
        self.connection, self.stream = connection, stream
        self.reader_stop = threading.Event()
        if recover and not self.modern:
            # Recreate the transport session, never replay a query that might
            # already have run. Handles from the previous backend stay expired.
            identifier = "proxy-initialize-" + secrets.token_hex(16)
            connection.settimeout(15)
            connection.sendall(encode({**self.initialize, "id": identifier}))
            while True:
                frame = read_frame(stream)
                if not frame:
                    raise ConnectionError("Backend closed while recreating its MCP session")
                value = decode(frame)
                if value.get("id") == identifier and "method" not in value:
                    if "error" in value:
                        raise RuntimeError("Backend session recovery failed: " + str(value["error"]))
                    result = value["result"]
                    for field in ("protocolVersion", "capabilities", "serverInfo"):
                        if result.get(field) != self.initialize_result.get(field):
                            raise RuntimeError("Backend capabilities changed; reconnect the MCP client")
                    break
                self.from_backend(value)
            if self.initialized:
                connection.sendall(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            connection.settimeout(None)
        self.generation += 1
        self.reader_thread = threading.Thread(target=self.read_backend,
            args=(self.generation, stream, self.reader_stop), daemon=True, name="backend-reader")
        self.reader_thread.start()

    def recover(self):
        if not self.modern and self.initialize_result is None:
            raise ConnectionError("Shared backend stopped during MCP initialization")
        for identifier in self.pending:
            self.emit({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32010,
                "message": "BACKEND_RESTARTED: query interrupted; repeat it and rediscover expired handles"}})
        self.pending.clear()
        self.server_requests.clear()
        self.disconnect()
        print("Shared backend disconnected; recreating the session without replaying queries", file=sys.stderr)
        self.attach(recover=True)

    def from_backend(self, value):
        if "id" in value:
            if "method" in value:
                if len(self.server_requests) >= 128:
                    raise RuntimeError("Backend exceeded the pending server-request limit")
                identifier = "proxy-request-" + secrets.token_hex(16)
                self.server_requests[identifier] = value["id"]
                value = {**value, "id": identifier}
            else:
                self.pending.discard(value["id"])
                if self.initialize_result is None and self.initialize is not None and value["id"] == self.initialize["id"] and "result" in value:
                    self.initialize_result = value["result"]
        self.emit(value)

    def from_stdin(self, frame):
        value = decode(frame)
        if "method" in value:
            stamp = value.get("params", {}).get("_meta", {}).get("io.modelcontextprotocol/protocolVersion")
            if value["method"] == "server/discover" or stamp == "2026-07-28":
                self.modern = True
            if "id" in value:
                identifier = value["id"]
                if identifier in self.pending or len(self.pending) >= 128:
                    raise ValueError("Duplicate request ID or too many pending client requests")
                self.pending.add(identifier)
                if value["method"] == "initialize":
                    if self.initialize is not None:
                        raise ValueError("This stdio connection already has an MCP initialization request")
                    self.initialize = value
            elif value["method"] == "notifications/initialized":
                self.initialized = True
            elif value["method"] == "notifications/cancelled":
                self.pending.discard(value.get("params", {}).get("requestId"))
        elif "id" in value:
            if value["id"] not in self.server_requests:
                print("Response targets an expired backend request", file=sys.stderr)
                return
            value = {**value, "id": self.server_requests.pop(value["id"])}
        try:
            self.connection.sendall(encode(value))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.recover()

    def run(self):
        try:
            self.attach()
            threading.Thread(target=self.stdin, daemon=True, name="stdin-reader").start()
            while True:
                kind, generation, value = self.events.get()
                if kind == "stdin_eof":
                    return
                if kind == "stdin_error":
                    raise value
                if kind == "stdin":
                    self.from_stdin(value)
                elif generation == self.generation:
                    if kind == "backend":
                        self.from_backend(decode(value))
                    elif kind == "backend_error":
                        print(f"Shared backend transport failed: {value}", file=sys.stderr)
                        self.recover()
                    elif kind == "backend_eof":
                        self.recover()
        finally:
            self.closed.set()
            self.disconnect()
