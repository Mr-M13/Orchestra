"""Client for the shadow-only authenticated authority service.

The client sends only an operation, a request id and an operation payload.  It
cannot assert a principal, role or uid; the server derives those from the Unix
peer credential.
"""

from __future__ import annotations

import json
import secrets
import socket
from pathlib import Path
from typing import Any

from orchestra.contracts import canonical_json


PROTOCOL_VERSION = 1
_MAX_RESPONSE_BYTES = 4 * 1_048_576


class AuthorityClientError(RuntimeError):
    """A refused request or an unavailable authority service."""

    def __init__(
        self, code: str, message: str, *, response: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.response = response
        self.request_id = request_id


def _as_dict(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return payload
    raise TypeError(f"{field} must be a mapping or a contract object")


class AuthorityClient:
    """One-request-per-connection client using the authority Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout: float = 30.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.last_request_id: str | None = None

    def call(
        self,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> Any:
        if not isinstance(operation, str) or not operation:
            raise AuthorityClientError("client_error", "operation is required")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise AuthorityClientError("client_error", "payload must be a mapping")
        request_id = request_id or f"req-{secrets.token_hex(16)}"
        self.last_request_id = request_id
        envelope = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": operation,
            "payload": payload,
        }
        try:
            response = self._exchange(envelope)
        except OSError as error:
            raise AuthorityClientError(
                "client_connection",
                f"cannot reach authority socket: {error}",
                request_id=request_id,
            ) from None

        if not isinstance(response, dict):
            raise AuthorityClientError(
                "protocol_error", "authority returned a non-object response",
                request_id=request_id,
            )
        if response.get("version") != PROTOCOL_VERSION:
            raise AuthorityClientError(
                "protocol_error", "authority protocol version mismatch",
                response=response, request_id=request_id,
            )
        if response.get("request_id") != request_id:
            # Parse-time refusals have no trustworthy request id.  They are
            # still safe to surface as errors, but never as success.
            if response.get("ok") is False and response.get("request_id") == "":
                pass
            else:
                raise AuthorityClientError(
                    "protocol_error", "authority response request_id mismatch",
                    response=response, request_id=request_id,
                )
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error")
        if not isinstance(error, dict):
            raise AuthorityClientError(
                "protocol_error", "authority error response is malformed",
                response=response, request_id=request_id,
            )
        raise AuthorityClientError(
            str(error.get("code", "authority_error")),
            str(error.get("message", "authority refused the request")),
            response=response,
            request_id=request_id,
        )

    def _exchange(self, envelope: dict[str, Any]) -> dict[str, Any]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(canonical_json(envelope).encode("utf-8") + b"\n")
            buffer = bytearray()
            while len(buffer) <= self.max_response_bytes:
                chunk = sock.recv(8_192)
                if not chunk:
                    break
                buffer.extend(chunk)
                if b"\n" in buffer:
                    line, _rest = bytes(buffer).split(b"\n", 1)
                    try:
                        return json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise AuthorityClientError(
                            "protocol_error",
                            "authority response is not valid JSON",
                        ) from None
            if len(buffer) > self.max_response_bytes:
                raise AuthorityClientError(
                    "protocol_error", "authority response is too large",
                )
            raise AuthorityClientError(
                "protocol_error", "authority closed without a complete response",
            )
        finally:
            sock.close()

    # -- reads -------------------------------------------------------------

    def health(self, *, request_id: str | None = None) -> dict[str, Any]:
        return self.call("health", {}, request_id=request_id)

    def proposal(
        self, proposal_digest: str, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "proposal", {"proposal_digest": proposal_digest}, request_id=request_id,
        )

    def approvals(
        self, proposal_digest: str, *, request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.call(
            "approvals", {"proposal_digest": proposal_digest}, request_id=request_id,
        )

    def attempt(
        self, attempt_id: str, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "attempt", {"attempt_id": attempt_id}, request_id=request_id,
        )

    def attempts_for_proposal(
        self, proposal_digest: str, *, request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.call(
            "attempts_for_proposal", {"proposal_digest": proposal_digest},
            request_id=request_id,
        )

    def grant(
        self, grant_id: str, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call("grant", {"grant_id": grant_id}, request_id=request_id)

    def validate_grant(
        self, task: Any, grant: Any, *, adapter_id: str, runtime: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "validate_grant",
            {
                "task": _as_dict(task, "task"),
                "grant": _as_dict(grant, "grant"),
                "adapter_id": adapter_id,
                "runtime": runtime,
            },
            request_id=request_id,
        )

    def verifications(
        self, attempt_id: str, *, request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.call(
            "verifications", {"attempt_id": attempt_id}, request_id=request_id,
        )

    def events(
        self, *, limit: int = 200, request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.call("events", {"limit": limit}, request_id=request_id)

    def service_events(
        self, *, limit: int = 200, request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.call("service_events", {"limit": limit}, request_id=request_id)

    def request_status(
        self, target_request_id: str, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "request_status", {"target_request_id": target_request_id},
            request_id=request_id,
        )

    # -- mutations ---------------------------------------------------------

    def propose(
        self, task: Any, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "propose", {"task": _as_dict(task, "task")}, request_id=request_id,
        )

    def decide_approval(
        self,
        proposal_digest: str,
        *,
        decision: str,
        reason: str | None = None,
        expires_at: str | None = None,
        approval_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "proposal_digest": proposal_digest,
            "decision": decision,
        }
        if reason is not None:
            payload["reason"] = reason
        if expires_at is not None:
            payload["expires_at"] = expires_at
        if approval_id is not None:
            payload["approval_id"] = approval_id
        return self.call("decide_approval", payload, request_id=request_id)

    def request_execution(
        self,
        task: Any,
        *,
        adapter_id: str,
        runtime: str,
        idempotency_key: str,
        grant_ttl_seconds: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task": _as_dict(task, "task"),
            "adapter_id": adapter_id,
            "runtime": runtime,
            "idempotency_key": idempotency_key,
        }
        if grant_ttl_seconds is not None:
            payload["grant_ttl_seconds"] = grant_ttl_seconds
        return self.call("request_execution", payload, request_id=request_id)

    def consume_grant(
        self,
        task: Any,
        grant: Any,
        *,
        adapter_id: str,
        runtime: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "consume_grant",
            {
                "task": _as_dict(task, "task"),
                "grant": _as_dict(grant, "grant"),
                "adapter_id": adapter_id,
                "runtime": runtime,
            },
            request_id=request_id,
        )

    def record_dispatch(
        self,
        attempt_id: str,
        *,
        provider_run_id: str,
        adapter_id: str,
        runtime: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "record_dispatch",
            {
                "attempt_id": attempt_id,
                "provider_run_id": provider_run_id,
                "adapter_id": adapter_id,
                "runtime": runtime,
            },
            request_id=request_id,
        )

    def record_report(
        self, report: Any, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "record_report", {"report": _as_dict(report, "report")},
            request_id=request_id,
        )

    def record_verification(
        self, verification: Any, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "record_verification",
            {"verification": _as_dict(verification, "verification")},
            request_id=request_id,
        )

    def cancel_attempt(
        self,
        attempt_id: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"attempt_id": attempt_id, "reason": reason}
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return self.call("cancel_attempt", payload, request_id=request_id)

    def reconcile_unknown(
        self,
        attempt_id: str,
        *,
        resolution: str,
        reason: str,
        evidence_ref: str | None = None,
        report: Any | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "resolution": resolution,
            "reason": reason,
        }
        if evidence_ref is not None:
            payload["evidence_ref"] = evidence_ref
        if report is not None:
            payload["report"] = _as_dict(report, "report")
        return self.call("reconcile_unknown", payload, request_id=request_id)

    def sweep(
        self,
        *,
        stale_after_seconds: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if stale_after_seconds is not None:
            payload["stale_after_seconds"] = stale_after_seconds
        return self.call("sweep", payload, request_id=request_id)
