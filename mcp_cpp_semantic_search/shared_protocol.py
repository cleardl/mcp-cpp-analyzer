"""Private loopback framing and mutual authentication (no MCP dependencies)."""
import hashlib
import hmac
import json
import secrets
import socket

PROTOCOL = 1
MAX_FRAME = 16 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def decode(data):
    def invalid_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")
    value = json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("A protocol frame must be a JSON object")
    return value


def proof(token, role, server_nonce, client_nonce):
    return hmac.new(bytes.fromhex(token), encode([role, server_nonce, client_nonce]), hashlib.sha256).hexdigest()


def read_frame(stream):
    value = stream.readline(MAX_FRAME + 1)
    if len(value) > MAX_FRAME:
        raise ValueError("Protocol frame exceeds 16 MiB")
    if value and not value.endswith(b"\n"):
        raise ConnectionError("Backend closed during a protocol frame")
    return value


def connect(endpoint):
    connection = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5)
    stream = connection.makefile("rb")
    try:
        hello = decode(read_frame(stream))
        if hello.get("protocol") != PROTOCOL or hello.get("instance_id") != endpoint["instance_id"]:
            raise ConnectionError("Shared-backend identity does not match its published endpoint")
        nonce = secrets.token_hex(32)
        connection.sendall(encode({"nonce": nonce, "proof": proof(endpoint["token"], "client", hello["nonce"], nonce)}))
        answer = decode(read_frame(stream))
        expected = proof(endpoint["token"], "server", hello["nonce"], nonce)
        if not hmac.compare_digest(answer.get("proof", ""), expected):
            raise ConnectionError("Shared-backend authentication failed")
        connection.settimeout(None)
        return connection, stream
    except BaseException:
        stream.close()
        connection.close()
        raise
