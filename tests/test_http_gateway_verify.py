from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from omcp.http_gateway import Gateway, GatewayServer, make_handler
from omcp.http_gateway_verify import (
    Response, VerificationError, _rpc, read_token_file, verify,
)
from tests.test_http_gateway import FakeBackend, TOKEN


class GatewayVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "omcp.sock"
        self.backend = FakeBackend(self.path)
        self.path.chmod(0o600)
        self.gateway = Gateway(self.path, TOKEN)
        self.server = GatewayServer(("127.0.0.1", 0), make_handler(self.gateway))
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.gateway.shutdown()
        self.backend.close()
        self.temp.cleanup()

    def test_verifier_checks_json_rpc_results_and_session_deletion(self):
        result = verify("127.0.0.1", self.server.server_port, TOKEN, cycles=2)
        self.assertEqual(result, {"cycles": 2, "status": "ok"})

    def test_verifier_rejects_a_gateway_that_accepts_any_long_token(self):
        class WeakGateway(Gateway):
            def allowed(self, header):
                return bool(
                    header
                    and header.startswith("Bearer ")
                    and len(header[7:]) >= 32
                )

        gateway = WeakGateway(self.path, TOKEN)
        server = GatewayServer(("127.0.0.1", 0), make_handler(gateway))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(VerificationError):
                verify("127.0.0.1", server.server_port, TOKEN, cycles=1)
        finally:
            gateway.shutdown()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_verifier_rejects_http_200_json_rpc_error(self):
        with self.assertRaises(VerificationError):
            _rpc(Response(
                200,
                json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32002, "message": "not initialized"},
                }).encode(),
                {},
            ), 1)

    def test_verifier_rejects_mismatched_ids_and_missing_results(self):
        with self.assertRaises(VerificationError):
            _rpc(Response(200, b'{"jsonrpc":"2.0","id":2,"result":{}}', {}), 1)
        with self.assertRaises(VerificationError):
            _rpc(Response(200, b'{"jsonrpc":"2.0","id":1}', {}), 1)

    def test_token_file_requires_exactly_one_safe_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp-http.env"
            path.write_text("ORCHESTRA_MCP_HTTP_TOKEN=" + "x" * 40 + "\n")
            self.assertEqual(read_token_file(path), "x" * 40)
            for value in ("short", "x" * 40 + "\nORCHESTRA_MCP_HTTP_TOKEN=" + "y" * 40,
                          "x" * 39 + "\x01"):
                path.write_text("ORCHESTRA_MCP_HTTP_TOKEN=" + value + "\n")
                with self.assertRaises(VerificationError):
                    read_token_file(path)
            path.write_text(
                "PYTHONPATH=/tmp\nORCHESTRA_MCP_HTTP_TOKEN=" + "x" * 40 + "\n",
            )
            with self.assertRaises(VerificationError):
                read_token_file(path)


if __name__ == "__main__":
    unittest.main()
