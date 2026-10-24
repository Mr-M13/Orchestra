"""Read-only live verifier for the loopback HTTP MCP gateway.

This module uses explicit checks rather than assert statements so it cannot be
disabled by PYTHONOPTIMIZE, and it validates JSON-RPC IDs/results rather than
only HTTP status codes.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from omcp.http_gateway import TOKEN_RE


class VerificationError(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def read_token_file(path: Path) -> str:
    values = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("ORCHESTRA_MCP_HTTP_TOKEN="):
            values.append(line.split("=", 1)[1])
    _require(len(values) == 1, "gateway environment must define exactly one token")
    token = values[0]
    _require(TOKEN_RE.fullmatch(token) is not None, "gateway token is invalid")
    return token


def _request(
    host: str, port: int, method: str, path: str, body: bytes | None = None,
    headers: dict[str, str] | None = None, timeout: float = 10.0,
) -> Response:
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        status = response.status
        connection.close()
    except (OSError, ValueError) as exc:
        raise VerificationError(
            f"HTTP request failed ({type(exc).__name__})"
        ) from None
    return Response(status, data, response_headers)


def _json_body(response: Response) -> dict:
    _require(response.status == 200, f"unexpected HTTP status {response.status}")
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise VerificationError("response is not valid JSON") from None
    _require(isinstance(value, dict), "response is not a JSON object")
    return value


def _rpc(response: Response, request_id: int) -> dict:
    value = _json_body(response)
    _require(value.get("jsonrpc") == "2.0", "invalid JSON-RPC version")
    _require(value.get("id") == request_id, "JSON-RPC response id mismatch")
    _require("error" not in value, "JSON-RPC response contains an error")
    result = value.get("result")
    _require(isinstance(result, dict), "JSON-RPC response has no object result")
    return result


def verify(host: str, port: int, token: str, cycles: int = 20) -> dict:
    _require(TOKEN_RE.fullmatch(token) is not None, "gateway token is invalid")
    _require(cycles >= 1, "cycle count must be positive")
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }

    health = _json_body(_request(host, port, "GET", "/healthz"))
    _require(health.get("status") == "ok", "health endpoint is not ok")
    _require(_request(host, port, "GET", "/readyz").status == 401,
             "readyz must require authentication")
    ready = _json_body(_request(host, port, "GET", "/readyz", headers=headers))
    _require(ready.get("ready") is True, "gateway backend is not ready")
    _require(
        _request(host, port, "POST", "/mcp", b"{}", {
            "Authorization": "Bearer wrong-token",
            "Content-Type": "application/json",
        }).status == 401,
        "wrong bearer token was accepted",
    )

    for cycle in range(cycles):
        initialize = _request(
            host, port, "POST", "/mcp",
            json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }).encode(), headers,
        )
        result = _rpc(initialize, 1)
        _require(
            result.get("protocolVersion") == "2025-06-18",
            "gateway negotiated an unexpected protocol version",
        )
        session = initialize.headers.get("mcp-session-id", "")
        _require(re.fullmatch(r"[0-9a-f]{32}", session) is not None,
                 "gateway did not issue a valid session id")
        session_headers = {**headers, "Mcp-Session-Id": session}

        notification = _request(
            host, port, "POST", "/mcp",
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            session_headers,
        )
        _require(notification.status == 200, "initialized notification failed")

        tools = _rpc(_request(
            host, port, "POST", "/mcp",
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
            session_headers,
        ), 2)
        _require(isinstance(tools.get("tools"), list) and tools["tools"],
                 "tools/list returned no tools")

        bridge = _rpc(_request(
            host, port, "POST", "/mcp",
            json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "bridge_status", "arguments": {}},
            }).encode(), session_headers,
        ), 3)
        structured = bridge.get("structuredContent")
        _require(
            isinstance(structured, dict) and structured.get("service") == "omcp",
            "bridge_status did not return the OMCP service identity",
        )

        _require(_request(
            host, port, "DELETE", "/mcp", headers=session_headers,
        ).status == 204, "session deletion failed")
        _require(_request(
            host, port, "POST", "/mcp",
            b'{"jsonrpc":"2.0","id":4,"method":"tools/list"}',
            session_headers,
        ).status == 400, "deleted session remained usable")
    return {"cycles": cycles, "status": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the loopback HTTP MCP gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    token = os.environ.get("ORCHESTRA_MCP_HTTP_TOKEN", "")
    if not token and args.env_file is not None:
        try:
            token = read_token_file(args.env_file)
        except (OSError, VerificationError) as exc:
            print(f"gateway verification failed: {exc}", file=sys.stderr)
            return 1
    try:
        result = verify(args.host, args.port, token, args.cycles)
    except VerificationError as exc:
        print(f"gateway verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"MCP HTTP verification passed: {result['cycles']} authenticated cycles",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
