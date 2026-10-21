"""Authenticated Unix-domain authority service (shadow-only Phase 3A).

The service runs in its own process under a dedicated Unix identity and derives
the caller's role from the kernel peer credential of the connected socket.  A
caller cannot select a principal, role or authority policy in the request body.

This module is intentionally not imported by OMCP or any live worker.  It is a
small standalone service used by the disposable Linux boundary test.
"""

from __future__ import annotations

import errno
import fcntl
import grp
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from orchestra.authority import (
    AuthorityPolicy,
    ExecutionAudience,
    Principal,
    PrototypeExecutionAuthority,
    StandingAuthorization,
    execution_grant_from_dict,
    execution_report_from_dict,
    task_spec_from_dict,
    verification_record_from_dict,
)
from orchestra.contracts import ContractValidationError, canonical_json
from orchestra.peercred import (
    PeerCredentialError,
    PeerCredentials,
    peer_credentials,
)


PROTOCOL_VERSION = 1
SERVICE_ROLES = frozenset({
    "coordinator", "human", "verifier", "operator", "worker",
})
READ_ROLES = frozenset({"coordinator", "human", "verifier", "operator"})
ALL_ROLES = SERVICE_ROLES
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "principal", "principal_id", "role", "uid", "peer_uid", "requester",
    "approver", "verifier", "worker", "consumer", "policy", "policies",
    "standing_authorizations", "service_uid", "authority_id",
})
OPERATION_ROLES: dict[str, frozenset[str]] = {
    "health": ALL_ROLES,
    "proposal": READ_ROLES,
    "approvals": READ_ROLES,
    "attempt": READ_ROLES,
    "attempts_for_proposal": READ_ROLES,
    "grant": READ_ROLES,
    "validate_grant": READ_ROLES,
    "verifications": READ_ROLES,
    "events": frozenset({"coordinator", "operator"}),
    "service_events": frozenset({"operator"}),
    "request_status": ALL_ROLES,
    "propose": frozenset({"coordinator"}),
    "decide_approval": frozenset({"human"}),
    "request_execution": frozenset({"coordinator"}),
    "consume_grant": frozenset({"coordinator"}),
    "record_dispatch": frozenset({"worker"}),
    "record_report": frozenset({"worker"}),
    "record_verification": frozenset({"verifier"}),
    "cancel_attempt": frozenset({"coordinator", "operator"}),
    "reconcile_unknown": frozenset({"operator"}),
    "sweep": frozenset({"operator"}),
}
_REQUIRED_ROLES = frozenset({"coordinator", "human", "verifier", "operator"})

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@ -]{0,63}$")

_SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rpc_requests (
    peer_uid INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('in_progress', 'succeeded', 'failed', 'unknown')
    ),
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (peer_uid, request_id)
);
CREATE INDEX IF NOT EXISTS rpc_requests_status
    ON rpc_requests(status);
CREATE TABLE IF NOT EXISTS service_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    peer_uid INTEGER,
    request_id TEXT,
    detail_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS service_events_no_update
BEFORE UPDATE ON service_events
BEGIN
    SELECT RAISE(ABORT, 'service audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS service_events_no_delete
BEFORE DELETE ON service_events
BEGIN
    SELECT RAISE(ABORT, 'service audit events are append-only');
END;
"""


class ServiceConfigurationError(ValueError):
    """The configured identity, paths or peer policy are unsafe."""


class ServiceBoundaryError(RuntimeError):
    """The service cannot establish or protect its runtime boundary."""


class ServiceProtocolError(ValueError):
    """A request envelope is malformed or attempts to assert identity."""


class SocketPathError(ServiceBoundaryError):
    """The socket path is occupied by an unsafe or live object."""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ServiceConfigurationError(f"{field} must be an integer >= {minimum}")
    return value


def _safe_label(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ServiceConfigurationError(f"{field} is not a safe label")
    return value


@dataclass(frozen=True)
class PeerIdentity:
    """One OS identity allowed to connect, with exactly one authority role."""

    uid: int
    role: str
    label: str

    def __post_init__(self) -> None:
        _safe_int(self.uid, "peer uid", minimum=1)
        if self.role not in SERVICE_ROLES:
            raise ServiceConfigurationError("unknown peer role")
        _safe_label(self.label, "peer label")

    @property
    def principal_id(self) -> str:
        return f"uid:{self.uid}"

    def to_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "role": self.role, "label": self.label}


@dataclass(frozen=True)
class ServiceConfig:
    """Serializable owner configuration for the authority service."""

    authority_id: str
    service_uid: int
    database_path: str
    socket_path: str
    socket_gid: int
    peers: tuple[PeerIdentity, ...]
    audiences: tuple[ExecutionAudience, ...]
    standing_authorizations: tuple[StandingAuthorization, ...] = ()
    socket_mode: int = 0o660
    grant_ttl_seconds: int = 300
    approval_ttl_seconds: int = 3_600
    stale_attempt_seconds: int = 3_600
    max_message_bytes: int = 1_048_576
    socket_timeout_seconds: int = 30
    max_connections: int = 16

    def __post_init__(self) -> None:
        _safe_int(self.service_uid, "service_uid", minimum=1)
        if self.service_uid == 0:
            raise ServiceConfigurationError("authority must not run as root")
        if not isinstance(self.authority_id, str) or not self.authority_id:
            raise ServiceConfigurationError("authority_id must not be empty")
        if not Path(self.database_path).is_absolute():
            raise ServiceConfigurationError("database_path must be absolute")
        if not Path(self.socket_path).is_absolute():
            raise ServiceConfigurationError("socket_path must be absolute")
        if Path(self.database_path) == Path(self.socket_path):
            raise ServiceConfigurationError("database and socket paths must differ")
        _safe_int(self.socket_gid, "socket_gid", minimum=1)
        if self.socket_mode not in {0o600, 0o660}:
            raise ServiceConfigurationError(
                "socket_mode must be 0600 or 0660"
            )

        peers = tuple(self.peers)
        if not peers:
            raise ServiceConfigurationError("at least one peer identity is required")
        uids = [peer.uid for peer in peers]
        if len(set(uids)) != len(uids):
            raise ServiceConfigurationError("duplicate peer uid")
        if self.service_uid in uids:
            raise ServiceConfigurationError(
                "the authority service uid may not also be a client peer"
            )
        roles = {peer.role for peer in peers}
        missing = _REQUIRED_ROLES - roles
        if missing:
            raise ServiceConfigurationError(
                f"missing required service roles: {sorted(missing)}"
            )
        object.__setattr__(
            self, "peers", tuple(sorted(peers, key=lambda peer: peer.uid)),
        )

        audiences = tuple(self.audiences)
        if not audiences:
            raise ServiceConfigurationError("at least one execution audience is required")
        object.__setattr__(
            self, "audiences", tuple(sorted(audiences, key=lambda item: item.key())),
        )

        rules = tuple(self.standing_authorizations)
        ids = [rule.policy_id for rule in rules]
        if len(set(ids)) != len(ids):
            raise ServiceConfigurationError("duplicate standing policy id")
        object.__setattr__(
            self, "standing_authorizations",
            tuple(sorted(rules, key=lambda item: item.policy_id)),
        )

        for field, maximum in (
            ("grant_ttl_seconds", 3_600),
            ("approval_ttl_seconds", 604_800),
            ("stale_attempt_seconds", 604_800),
            ("max_message_bytes", 8_388_608),
            ("socket_timeout_seconds", 120),
            ("max_connections", 128),
        ):
            value = getattr(self, field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= maximum
            ):
                raise ServiceConfigurationError(
                    f"{field} must be between 1 and {maximum}"
                )
        if self.max_message_bytes < 1_024:
            raise ServiceConfigurationError("max_message_bytes is too small")

    @property
    def state_dir(self) -> Path:
        return Path(self.database_path).parent

    @property
    def runtime_dir(self) -> Path:
        return Path(self.socket_path).parent

    def peer(self, uid: int) -> PeerIdentity | None:
        return next((peer for peer in self.peers if peer.uid == uid), None)

    def to_authority_policy(self) -> AuthorityPolicy:
        return AuthorityPolicy(
            authority_id=self.authority_id,
            trusted_humans=frozenset(
                peer.principal_id for peer in self.peers if peer.role == "human"
            ),
            trusted_verifiers=frozenset(
                peer.principal_id for peer in self.peers if peer.role == "verifier"
            ),
            trusted_operators=frozenset(
                peer.principal_id for peer in self.peers if peer.role == "operator"
            ),
            audiences=self.audiences,
            standing_authorizations=self.standing_authorizations,
            grant_ttl_seconds=self.grant_ttl_seconds,
            approval_ttl_seconds=self.approval_ttl_seconds,
            stale_attempt_seconds=self.stale_attempt_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "service_uid": self.service_uid,
            "database_path": self.database_path,
            "socket_path": self.socket_path,
            "socket_gid": self.socket_gid,
            "socket_mode": self.socket_mode,
            "peers": [peer.to_dict() for peer in self.peers],
            "audiences": [audience.to_dict() for audience in self.audiences],
            "standing_authorizations": [
                rule.to_dict() for rule in self.standing_authorizations
            ],
            "grant_ttl_seconds": self.grant_ttl_seconds,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "stale_attempt_seconds": self.stale_attempt_seconds,
            "max_message_bytes": self.max_message_bytes,
            "socket_timeout_seconds": self.socket_timeout_seconds,
            "max_connections": self.max_connections,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ServiceConfig":
        if not isinstance(payload, dict):
            raise ServiceConfigurationError("service config must be a JSON object")
        required = {
            "authority_id", "service_uid", "database_path", "socket_path",
            "socket_gid", "peers", "audiences",
        }
        missing = required - set(payload)
        if missing:
            raise ServiceConfigurationError(
                f"service config is missing: {sorted(missing)}"
            )
        allowed = required | {
            "standing_authorizations", "socket_mode", "grant_ttl_seconds",
            "approval_ttl_seconds", "stale_attempt_seconds",
            "max_message_bytes", "socket_timeout_seconds", "max_connections",
        }
        extra = set(payload) - allowed
        if extra:
            raise ServiceConfigurationError(
                f"service config has unknown keys: {sorted(extra)}"
            )
        peers = payload["peers"]
        if not isinstance(peers, list):
            raise ServiceConfigurationError("peers must be a list")
        audiences = payload["audiences"]
        if not isinstance(audiences, list):
            raise ServiceConfigurationError("audiences must be a list")
        rules = payload.get("standing_authorizations", [])
        if not isinstance(rules, list):
            raise ServiceConfigurationError("standing_authorizations must be a list")
        try:
            return cls(
                authority_id=payload["authority_id"],
                service_uid=payload["service_uid"],
                database_path=payload["database_path"],
                socket_path=payload["socket_path"],
                socket_gid=payload["socket_gid"],
                peers=tuple(PeerIdentity(**peer) for peer in peers),
                audiences=tuple(ExecutionAudience(**item) for item in audiences),
                standing_authorizations=tuple(
                    StandingAuthorization(
                        **{
                            **rule,
                            "allowed_operations": tuple(
                                rule.get("allowed_operations", ())
                            ),
                            "write_roots": tuple(rule.get("write_roots", ())),
                        }
                    )
                    for rule in rules
                ),
                socket_mode=payload.get("socket_mode", 0o660),
                grant_ttl_seconds=payload.get("grant_ttl_seconds", 300),
                approval_ttl_seconds=payload.get("approval_ttl_seconds", 3_600),
                stale_attempt_seconds=payload.get("stale_attempt_seconds", 3_600),
                max_message_bytes=payload.get("max_message_bytes", 1_048_576),
                socket_timeout_seconds=payload.get("socket_timeout_seconds", 30),
                max_connections=payload.get("max_connections", 16),
            )
        except (TypeError, ValueError) as error:
            raise ServiceConfigurationError(str(error)) from None

    @classmethod
    def load(cls, path: str | Path) -> "ServiceConfig":
        file_path = Path(path)
        if not file_path.is_file() or file_path.stat().st_size > 1_048_576:
            raise ServiceConfigurationError("config file is missing or too large")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ServiceConfigurationError(f"cannot read config: {error}") from None
        return cls.from_dict(payload)

    def canonical_payload(self) -> dict[str, Any]:
        return self.to_dict()

    @property
    def config_digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.canonical_payload()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ServiceStatus:
    authority_id: str
    policy_digest: str
    service_uid: int
    service_gid: int
    pid: int
    protocol_version: int
    peer_uid: int
    peer_role: str
    database_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "policy_digest": self.policy_digest,
            "service_uid": self.service_uid,
            "service_gid": self.service_gid,
            "pid": self.pid,
            "protocol_version": self.protocol_version,
            "peer_uid": self.peer_uid,
            "peer_role": self.peer_role,
            "database_ready": self.database_ready,
        }


def _error_code(error: BaseException) -> str:
    name = type(error).__name__
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return snake[:-6] if snake.endswith("_error") else snake


def ok_response(request_id: str, result: Any) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


def error_response(request_id: str, code: str, message: str) -> dict[str, Any]:
    text = str(message).replace("\x00", "")[:2_000] or code
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": text},
    }


def _request_digest(operation: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({"operation": operation, "payload": payload}).encode("utf-8")
    ).hexdigest()


def _required_keys(
    payload: dict[str, Any], required: set[str], optional: set[str] | None = None,
) -> None:
    if not isinstance(payload, dict):
        raise ServiceProtocolError("payload must be an object")
    optional = optional or set()
    missing = required - set(payload)
    if missing:
        raise ServiceProtocolError(f"payload is missing: {sorted(missing)}")
    extra = set(payload) - required - optional
    if extra:
        raise ServiceProtocolError(f"payload has unknown keys: {sorted(extra)}")
    forbidden = set(payload) & FORBIDDEN_PAYLOAD_KEYS
    if forbidden:
        raise ServiceProtocolError(
            f"caller identity and policy fields are forbidden: {sorted(forbidden)}"
        )


def _string(payload: dict[str, Any], key: str, *, maximum: int = 2_000) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ServiceProtocolError(f"{key} must be a bounded string")
    if "\x00" in value:
        raise ServiceProtocolError(f"{key} must not contain NUL")
    return value


class AuthorityService:
    """One-process, one-socket, peer-credentialed authority service."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        credential_reader: Callable[[socket.socket], PeerCredentials] = peer_credentials,
    ) -> None:
        if not isinstance(config, ServiceConfig):
            raise ServiceConfigurationError("config must be ServiceConfig")
        self._config = config
        self._clock = clock
        self._credential_reader = credential_reader
        self._stop_event = threading.Event()
        self._started = False
        self._stopping = False
        self._state_lock = threading.RLock()
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._connection_slots = threading.BoundedSemaphore(
            config.max_connections
        )
        self._server_socket: socket.socket | None = None
        self._socket_inode: tuple[int, int] | None = None
        self._lock_fd: int | None = None
        self._lock_path: Path | None = None
        self._authority: PrototypeExecutionAuthority | None = None
        self._logger = logging.getLogger("orchestra.authority_service")

    @property
    def config(self) -> ServiceConfig:
        return self._config

    @property
    def authority(self) -> PrototypeExecutionAuthority:
        if self._authority is None:
            raise ServiceBoundaryError("service has not started")
        return self._authority

    # -- startup and shutdown ---------------------------------------------

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                raise ServiceBoundaryError("service is already started")
            self._enforce_process_identity()
            self._prepare_directories()
            self._acquire_lock()
            try:
                self._authority = PrototypeExecutionAuthority(
                    self._config.database_path,
                    self._config.to_authority_policy(),
                    clock=self._clock,
                )
                self._secure_database_files()
                self._initialise_service_state()
                recovery = self._authority.recover_after_restart()
                recovered = self._recover_interrupted_requests()
                self._bind_socket()
                self._service_event(
                    "service_started",
                    detail={
                        "pid": os.getpid(),
                        "uid": os.geteuid(),
                        "policy_digest": self._authority.policy.policy_digest,
                        "expired_grants": list(recovery.expired_grants),
                        "unknown_attempts": list(recovery.unknown_attempts),
                        "recovered_requests": recovered,
                    },
                )
                self._started = True
                self._logger.info(
                    "authority service started as uid=%s socket=%s",
                    os.geteuid(), self._config.socket_path,
                )
            except BaseException:
                self._cleanup_failed_start()
                raise

    def serve_forever(self) -> None:
        if not self._started:
            self.start()
        if self._server_socket is None:
            raise ServiceBoundaryError("listening socket is unavailable")
        self._server_socket.settimeout(0.5)
        while not self._stop_event.is_set():
            try:
                connection, _address = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    break
                raise
            try:
                credentials = self._credential_reader(connection)
            except PeerCredentialError as error:
                self._logger.warning("rejecting connection without peer identity: %s", error)
                connection.close()
                continue
            if not self._connection_slots.acquire(blocking=False):
                self._send(
                    connection,
                    error_response("", "service_busy", "too many connections"),
                )
                connection.close()
                continue
            thread = threading.Thread(
                target=self._connection_worker,
                args=(connection, credentials),
                name=f"authority-peer-{credentials.uid}",
                daemon=True,
            )
            with self._threads_lock:
                self._threads.add(thread)
            try:
                thread.start()
            except BaseException:
                with self._threads_lock:
                    self._threads.discard(thread)
                self._connection_slots.release()
                connection.close()
                raise

    def request_stop(self) -> None:
        self._stop_event.set()
        listener = self._server_socket
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def stop(self) -> None:
        with self._state_lock:
            if self._stopping or not self._started:
                return
            self._stopping = True
        self.request_stop()
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=5)
        with self._threads_lock:
            self._threads.clear()
        try:
            if self._authority is not None:
                self._service_event("service_stopping", detail={"pid": os.getpid()})
        except (OSError, sqlite3.Error):
            self._logger.exception("could not append service shutdown event")
        self._unlink_socket_if_owned()
        self._release_lock()
        if self._authority is not None:
            self._authority.close()
        self._started = False
        self._stopping = False
        self._logger.info("authority service stopped")

    def _cleanup_failed_start(self) -> None:
        self.request_stop()
        self._unlink_socket_if_owned()
        self._release_lock()
        if self._authority is not None:
            self._authority.close()

    # -- boundary checks ---------------------------------------------------

    def _enforce_process_identity(self) -> None:
        actual_uid = os.geteuid()
        if actual_uid != self._config.service_uid:
            raise ServiceBoundaryError(
                f"service must run as uid {self._config.service_uid}, not {actual_uid}"
            )
        if actual_uid == 0:
            raise ServiceBoundaryError("authority service must not run as root")
        try:
            group = grp.getgrgid(self._config.socket_gid)
        except KeyError:
            raise ServiceBoundaryError(
                f"socket group gid {self._config.socket_gid} does not exist"
            ) from None
        if self._config.socket_mode == 0o660:
            groups = set(os.getgroups()) | {os.getegid()}
            if self._config.socket_gid not in groups:
                raise ServiceBoundaryError(
                    "authority uid must belong to the socket client group "
                    f"({group.gr_name}) to protect the socket"
                )

    def _prepare_directories(self) -> None:
        state_dir = self._config.state_dir
        runtime_dir = self._config.runtime_dir
        self._ensure_directory(state_dir, 0o700, group_access=False)
        self._ensure_directory(runtime_dir, 0o750, group_access=True)
        runtime_info = runtime_dir.stat()
        if (
            self._config.socket_mode == 0o660
            and runtime_info.st_gid != self._config.socket_gid
        ):
            raise ServiceBoundaryError(
                "runtime directory must be owned by the socket client group"
            )
        if runtime_info.st_dev != state_dir.stat().st_dev:
            # Not a security requirement, but makes ownership review explicit.
            self._logger.info("state and runtime directories use different filesystems")

    def _ensure_directory(
        self, path: Path, mode: int, *, group_access: bool,
    ) -> None:
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            path.mkdir(parents=True, mode=mode)
            os.chmod(path, mode)
            existing = os.lstat(path)
        if stat.S_ISLNK(existing.st_mode):
            raise ServiceBoundaryError(f"{path} must not be a symlink")
        if not stat.S_ISDIR(existing.st_mode):
            raise ServiceBoundaryError(f"{path} is not a directory")
        if existing.st_uid != os.geteuid():
            raise ServiceBoundaryError(
                f"{path} must be owned by the authority uid {os.geteuid()}"
            )
        actual = stat.S_IMODE(existing.st_mode)
        if group_access:
            if actual & 0o007:
                raise ServiceBoundaryError(f"{path} must not be accessible to others")
            if actual & 0o020:
                raise ServiceBoundaryError(f"{path} must not be group-writable")
        else:
            if actual & 0o077:
                raise ServiceBoundaryError(
                    f"{path} must have no group or other access"
                )

    def _secure_database_files(self) -> None:
        path = Path(self._config.database_path)
        if path.is_symlink():
            raise ServiceBoundaryError("authority database must not be a symlink")
        for candidate in (
            path, Path(f"{path}-wal"), Path(f"{path}-shm"),
        ):
            try:
                if candidate.exists() and candidate.is_file():
                    os.chmod(candidate, 0o600)
            except OSError as error:
                raise ServiceBoundaryError(
                    f"cannot secure database file {candidate}: {error}"
                ) from None
        if path.stat().st_uid != os.geteuid():
            raise ServiceBoundaryError("database must be owned by the authority uid")

    def _acquire_lock(self) -> None:
        lock_path = self._config.runtime_dir / "authority.lock"
        fd: int | None = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError as error:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise ServiceBoundaryError(
                f"another authority instance holds {lock_path}: {error}"
            ) from None
        self._lock_fd = fd
        self._lock_path = lock_path

    def _release_lock(self) -> None:
        fd = self._lock_fd
        path = self._lock_path
        self._lock_fd = None
        self._lock_path = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                self._logger.exception("could not remove authority lock file")

    def _bind_socket(self) -> None:
        path = Path(self._config.socket_path)
        self._remove_stale_socket(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.set_inheritable(False)
        try:
            listener.bind(str(path))
            info = os.lstat(path)
            self._socket_inode = (info.st_dev, info.st_ino)
            listener.listen(64)
            os.chmod(path, self._config.socket_mode)
            os.chown(path, os.geteuid(), self._config.socket_gid)
            info = os.lstat(path)
            if not stat.S_ISSOCK(info.st_mode):
                raise SocketPathError("bound path is not a socket")
            if info.st_uid != os.geteuid():
                raise SocketPathError("bound socket has an unexpected owner")
            if (info.st_dev, info.st_ino) != self._socket_inode:
                raise SocketPathError("socket path was replaced during binding")
            self._server_socket = listener
        except BaseException:
            listener.close()
            self._unlink_socket_if_owned()
            raise

    def _remove_stale_socket(self, path: Path) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise SocketPathError(f"refusing to follow symlink at {path}")
        if not stat.S_ISSOCK(info.st_mode):
            raise SocketPathError(f"refusing to replace non-socket at {path}")
        if info.st_uid != os.geteuid():
            raise SocketPathError(f"stale socket at {path} has another owner")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        except OSError as error:
            if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise SocketPathError(
                    f"cannot determine whether {path} is stale: {error}"
                ) from None
        else:
            raise SocketPathError(
                f"another authority service is already listening on {path}"
            )
        finally:
            probe.close()
        current = os.lstat(path)
        if (
            not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.geteuid()
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise SocketPathError(f"socket path changed while checking {path}")
        os.unlink(path)

    def _unlink_socket_if_owned(self) -> None:
        path = Path(self._config.socket_path)
        inode = self._socket_inode
        self._socket_inode = None
        listener = self._server_socket
        self._server_socket = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if inode is None:
            return
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(info.st_mode)
            and (info.st_dev, info.st_ino) == inode
        ):
            try:
                os.unlink(path)
            except OSError:
                self._logger.exception("could not unlink authority socket")

    # -- service durable state --------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._config.database_path, timeout=5.0, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialise_service_state(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_SERVICE_SCHEMA)
        finally:
            connection.close()

    def _recover_interrupted_requests(self) -> int:
        now = _iso_now()
        recovered = 0
        with self._service_transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM rpc_requests WHERE status = 'in_progress'"
            ).fetchall()
            for row in rows:
                response = error_response(
                    row["request_id"],
                    "request_outcome_unknown",
                    "the service restarted before recording this response; "
                    "query durable state before retrying",
                )
                connection.execute(
                    "UPDATE rpc_requests SET status = 'unknown', response_json = ?, "
                    "updated_at = ? WHERE peer_uid = ? AND request_id = ?",
                    (canonical_json(response), now, row["peer_uid"],
                     row["request_id"]),
                )
                connection.execute(
                    "INSERT INTO service_events(at, event, peer_uid, request_id, "
                    "detail_json) VALUES (?, ?, ?, ?, ?)",
                    (now, "request_recovered_unknown", row["peer_uid"],
                     row["request_id"],
                     canonical_json({"operation": row["operation"]})),
                )
                recovered += 1
        return recovered

    @contextmanager
    def _service_transaction(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        else:
            connection.execute("COMMIT")
            connection.close()

    @contextmanager
    def _service_read(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _service_event(
        self, event: str, *, peer_uid: int | None = None,
        request_id: str | None = None, detail: dict[str, Any] | None = None,
    ) -> None:
        with self._service_transaction() as connection:
            connection.execute(
                "INSERT INTO service_events(at, event, peer_uid, request_id, "
                "detail_json) VALUES (?, ?, ?, ?, ?)",
                (_iso_now(), event, peer_uid, request_id,
                 canonical_json(detail or {})),
            )

    def service_events(self) -> tuple[dict[str, Any], ...]:
        with self._service_read() as connection:
            rows = connection.execute(
                "SELECT * FROM service_events ORDER BY sequence"
            ).fetchall()
        return tuple(
            {
                "sequence": row["sequence"],
                "at": row["at"],
                "event": row["event"],
                "peer_uid": row["peer_uid"],
                "request_id": row["request_id"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        )

    # -- protocol ----------------------------------------------------------

    def _read_line(self, connection: socket.socket) -> bytes:
        limit = self._config.max_message_bytes
        buffer = bytearray()
        while len(buffer) <= limit:
            chunk = connection.recv(min(8_192, limit + 1 - len(buffer)))
            if not chunk:
                break
            buffer.extend(chunk)
            if b"\n" in buffer:
                line, _rest = bytes(buffer).split(b"\n", 1)
                return line
        if len(buffer) > limit:
            raise ServiceProtocolError("request exceeds max_message_bytes")
        if not buffer:
            raise ServiceProtocolError("empty request")
        raise ServiceProtocolError("request must end with a newline")

    def _send(self, connection: socket.socket, response: dict[str, Any]) -> None:
        try:
            payload = canonical_json(response).encode("utf-8") + b"\n"
            connection.sendall(payload)
        except OSError:
            pass

    def _connection_worker(
        self, connection: socket.socket, credentials: PeerCredentials,
    ) -> None:
        try:
            connection.settimeout(self._config.socket_timeout_seconds)
            peer = self._config.peer(credentials.uid)
            if peer is None:
                self._logger.warning(
                    "rejected unconfigured peer uid=%s pid=%s",
                    credentials.uid, credentials.pid,
                )
                self._send(
                    connection,
                    error_response("", "unauthorized_peer", "peer uid is not configured"),
                )
                try:
                    self._service_event(
                        "peer_rejected", peer_uid=credentials.uid,
                        detail={"pid": credentials.pid, "reason": "unknown_uid"},
                    )
                except sqlite3.Error:
                    self._logger.exception("could not record rejected peer")
                return

            try:
                line = self._read_line(connection)
                request = self._parse_envelope(line)
            except ServiceProtocolError as error:
                self._send(connection, error_response("", "invalid_request", str(error)))
                return
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(
                    connection,
                    error_response("", "invalid_json", "request is not valid JSON"),
                )
                return
            except (socket.timeout, TimeoutError):
                self._send(
                    connection,
                    error_response("", "request_timeout", "request timed out"),
                )
                return
            except OSError:
                return

            request_id = request["request_id"]
            operation = request["operation"]
            payload = request["payload"]
            allowed_roles = OPERATION_ROLES.get(operation)
            if allowed_roles is None:
                self._send(
                    connection,
                    error_response(request_id, "unknown_operation",
                                   "operation is not implemented"),
                )
                return
            if peer.role not in allowed_roles:
                self._send(
                    connection,
                    error_response(
                        request_id, "operation_not_permitted",
                        f"peer role {peer.role} may not perform {operation}",
                    ),
                )
                self._service_event(
                    "operation_denied", peer_uid=peer.uid, request_id=request_id,
                    detail={"role": peer.role, "operation": operation},
                )
                return

            principal = Principal(peer.principal_id, peer.role)
            response = self._execute_cached(
                peer, principal, request_id, operation, payload,
            )
            self._send(connection, response)
        except Exception:
            self._logger.exception("unhandled connection failure")
            self._send(
                connection,
                error_response("", "internal_error", "internal service error"),
            )
        finally:
            try:
                connection.close()
            finally:
                with self._threads_lock:
                    self._threads.discard(threading.current_thread())
                self._connection_slots.release()

    def _parse_envelope(self, line: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise
        if not isinstance(payload, dict):
            raise ServiceProtocolError("request must be a JSON object")
        allowed = {"version", "request_id", "operation", "payload"}
        if set(payload) - allowed:
            raise ServiceProtocolError("request envelope contains unknown keys")
        if payload.get("version") != PROTOCOL_VERSION:
            raise ServiceProtocolError("unsupported protocol version")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ServiceProtocolError("request_id is not a safe identifier")
        operation = payload.get("operation")
        if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
            raise ServiceProtocolError("operation is not a safe identifier")
        body = payload.get("payload", {})
        if not isinstance(body, dict):
            raise ServiceProtocolError("payload must be a JSON object")
        forbidden = set(body) & FORBIDDEN_PAYLOAD_KEYS
        if forbidden:
            raise ServiceProtocolError(
                f"caller identity and policy fields are forbidden: {sorted(forbidden)}"
            )
        return {
            "request_id": request_id,
            "operation": operation,
            "payload": body,
        }

    def _execute_cached(
        self, peer: PeerIdentity, principal: Principal, request_id: str,
        operation: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        digest = _request_digest(operation, payload)
        now = _iso_now()
        cached: str | None = None
        with self._service_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM rpc_requests WHERE peer_uid = ? AND request_id = ?",
                (peer.uid, request_id),
            ).fetchone()
            if row is not None:
                if row["operation"] != operation or row["request_digest"] != digest:
                    return error_response(
                        request_id, "request_id_conflict",
                        "request_id was already used with different parameters",
                    )
                if row["status"] in {"succeeded", "failed", "unknown"}:
                    return json.loads(row["response_json"])
                return error_response(
                    request_id, "request_in_progress",
                    "an identical request is already in progress",
                )
            connection.execute(
                "INSERT INTO rpc_requests(peer_uid, request_id, operation, "
                "request_digest, status, response_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'in_progress', NULL, ?, ?)",
                (peer.uid, request_id, operation, digest, now, now),
            )

        try:
            result = self._dispatch(principal, operation, payload, request_id)
            response = ok_response(request_id, result)
            status = "succeeded"
        except (
            ContractValidationError, ServiceProtocolError, KeyError, TypeError,
            ValueError,
        ) as error:
            response = error_response(request_id, "invalid_request", str(error))
            status = "failed"
        except Exception as error:
            code = _error_code(error)
            if code == "internal_error":
                self._logger.exception(
                    "authority operation failed uid=%s operation=%s request=%s",
                    peer.uid, operation, request_id,
                )
                message = "internal authority error"
            else:
                message = str(error) or code
            response = error_response(request_id, code, message)
            status = "failed"

        with self._service_transaction() as connection:
            connection.execute(
                "UPDATE rpc_requests SET status = ?, response_json = ?, "
                "updated_at = ? WHERE peer_uid = ? AND request_id = ?",
                (status, canonical_json(response), _iso_now(), peer.uid, request_id),
            )
        try:
            self._service_event(
                "request_completed", peer_uid=peer.uid, request_id=request_id,
                detail={
                    "role": peer.role,
                    "operation": operation,
                    "status": status,
                    "response_ok": response["ok"],
                    "error_code": response.get("error", {}).get("code"),
                },
            )
        except sqlite3.Error:
            self._logger.exception("could not record request audit event")
        return response

    def _dispatch(
        self, principal: Principal, operation: str, payload: dict[str, Any],
        request_id: str,
    ) -> Any:
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            raise ServiceProtocolError("operation is not implemented")
        return handler(principal, payload, request_id)

    # -- read operations ---------------------------------------------------

    def _op_health(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, set())
        return ServiceStatus(
            authority_id=self.authority.authority_id,
            policy_digest=self.authority.policy.policy_digest,
            service_uid=os.geteuid(),
            service_gid=os.getegid(),
            pid=os.getpid(),
            protocol_version=PROTOCOL_VERSION,
            peer_uid=int(principal.principal_id.split(":", 1)[1]),
            peer_role=principal.kind,
            database_ready=True,
        ).to_dict()

    def _op_proposal(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"proposal_digest"})
        return self.authority.proposal(
            _string(payload, "proposal_digest")
        ).to_dict()

    def _op_approvals(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> list[dict[str, Any]]:
        _required_keys(payload, {"proposal_digest"})
        return [
            approval.to_dict()
            for approval in self.authority.approvals(
                _string(payload, "proposal_digest")
            )
        ]

    def _op_attempt(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"attempt_id"})
        return self.authority.attempt(_string(payload, "attempt_id")).to_dict()

    def _op_attempts_for_proposal(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> list[dict[str, Any]]:
        _required_keys(payload, {"proposal_digest"})
        return [
            attempt.to_dict()
            for attempt in self.authority.attempts_for_proposal(
                _string(payload, "proposal_digest")
            )
        ]

    def _op_grant(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"grant_id"})
        return self.authority.grant(_string(payload, "grant_id")).to_dict()

    def _op_validate_grant(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(
            payload, {"task", "grant", "adapter_id", "runtime"},
        )
        task = task_spec_from_dict(payload["task"])
        grant = execution_grant_from_dict(payload["grant"])
        return self.authority.validate_grant(
            task, grant,
            adapter_id=_string(payload, "adapter_id"),
            runtime=_string(payload, "runtime"),
        ).to_dict()

    def _op_verifications(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> list[dict[str, Any]]:
        _required_keys(payload, {"attempt_id"})
        return [
            record.to_dict()
            for record in self.authority.verification_records(
                _string(payload, "attempt_id")
            )
        ]

    def _op_events(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> list[dict[str, Any]]:
        _required_keys(payload, set(), {"limit"})
        limit = payload.get("limit", 200)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ServiceProtocolError("limit must be an integer between 1 and 1000")
        return [event.to_dict() for event in self.authority.events()[-limit:]]

    def _op_service_events(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> list[dict[str, Any]]:
        _required_keys(payload, set(), {"limit"})
        limit = payload.get("limit", 200)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ServiceProtocolError("limit must be an integer between 1 and 1000")
        return list(self.service_events()[-limit:])

    def _op_request_status(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"target_request_id"})
        target = _string(payload, "target_request_id")
        uid = int(principal.principal_id.split(":", 1)[1])
        with self._service_read() as connection:
            row = connection.execute(
                "SELECT * FROM rpc_requests WHERE peer_uid = ? AND request_id = ?",
                (uid, target),
            ).fetchone()
        if row is None:
            return {"request_id": target, "status": "not_found", "response": None}
        response = json.loads(row["response_json"]) if row["response_json"] else None
        return {
            "request_id": target,
            "status": row["status"],
            "operation": row["operation"],
            "response": response,
        }

    # -- mutating operations ----------------------------------------------

    def _op_propose(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"task"})
        task = task_spec_from_dict(payload["task"])
        return self.authority.propose(task, proposer=principal).to_dict()

    def _op_decide_approval(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(
            payload, {"proposal_digest", "decision"},
            {"reason", "expires_at", "approval_id"},
        )
        return self.authority.decide_approval(
            _string(payload, "proposal_digest"),
            decision=_string(payload, "decision", maximum=32),
            approver=principal,
            approval_id=payload.get("approval_id", f"rpc-{request_id}"),
            reason=payload.get("reason"),
            expires_at=payload.get("expires_at"),
        ).to_dict()

    def _op_request_execution(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(
            payload, {"task", "adapter_id", "runtime", "idempotency_key"},
            {"grant_ttl_seconds"},
        )
        return self.authority.request_execution(
            task_spec_from_dict(payload["task"]),
            adapter_id=_string(payload, "adapter_id"),
            runtime=_string(payload, "runtime"),
            idempotency_key=_string(payload, "idempotency_key"),
            requester=principal,
            grant_ttl_seconds=payload.get("grant_ttl_seconds"),
        ).to_dict()

    def _op_consume_grant(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"task", "grant", "adapter_id", "runtime"})
        return self.authority.consume_grant(
            task_spec_from_dict(payload["task"]),
            execution_grant_from_dict(payload["grant"]),
            adapter_id=_string(payload, "adapter_id"),
            runtime=_string(payload, "runtime"),
            consumer=principal,
        ).to_dict()

    def _op_record_dispatch(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(
            payload, {"attempt_id", "provider_run_id", "adapter_id", "runtime"},
        )
        return self.authority.record_dispatch(
            _string(payload, "attempt_id"),
            provider_run_id=_string(payload, "provider_run_id"),
            adapter_id=_string(payload, "adapter_id"),
            runtime=_string(payload, "runtime"),
            worker=principal,
        ).to_dict()

    def _op_record_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"report"})
        return self.authority.record_report(
            execution_report_from_dict(payload["report"]),
            worker=principal,
        ).to_dict()

    def _op_record_verification(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"verification"})
        return self.authority.record_verification(
            verification_record_from_dict(payload["verification"]),
            verifier=principal,
        ).to_dict()

    def _op_cancel_attempt(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, {"attempt_id", "reason"}, {"idempotency_key"})
        return self.authority.cancel_attempt(
            _string(payload, "attempt_id"),
            principal=principal,
            reason=_string(payload, "reason"),
            idempotency_key=payload.get("idempotency_key", f"rpc-{request_id}"),
        ).to_dict()

    def _op_reconcile_unknown(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(
            payload, {"attempt_id", "resolution", "reason"},
            {"evidence_ref", "report"},
        )
        report = (
            execution_report_from_dict(payload["report"])
            if payload.get("report") is not None else None
        )
        return self.authority.reconcile_unknown(
            _string(payload, "attempt_id"),
            operator=principal,
            resolution=_string(payload, "resolution", maximum=32),
            reason=_string(payload, "reason"),
            report=report,
            evidence_ref=payload.get("evidence_ref"),
        ).to_dict()

    def _op_sweep(
        self, principal: Principal, payload: dict[str, Any], request_id: str,
    ) -> dict[str, Any]:
        _required_keys(payload, set(), {"stale_after_seconds"})
        return self.authority.sweep(
            stale_after_seconds=payload.get("stale_after_seconds")
        ).to_dict()


def _validate_config_file(config_path: Path, config: ServiceConfig) -> None:
    try:
        info = os.lstat(config_path)
    except OSError as error:
        raise ServiceConfigurationError(f"cannot stat config file: {error}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ServiceConfigurationError("config file must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ServiceConfigurationError("config path must be a regular file")
    if info.st_uid not in {0, config.service_uid}:
        raise ServiceConfigurationError(
            "config file must be owned by root or the authority service uid"
        )
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ServiceConfigurationError(
            "config file must not be writable by group or others"
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the shadow-only Orchestra authority service",
    )
    parser.add_argument("--config", required=True, help="path to service JSON config")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s authority-service %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        config = ServiceConfig.load(args.config)
        _validate_config_file(Path(args.config), config)
    except ServiceConfigurationError as error:
        print(f"authority service configuration error: {error}", file=sys.stderr)
        return 2
    if args.check:
        print(
            json.dumps(
                {
                    "ok": True,
                    "authority_id": config.authority_id,
                    "config_digest": config.config_digest,
                    "peer_count": len(config.peers),
                    "socket_path": config.socket_path,
                    "database_path": config.database_path,
                },
                sort_keys=True,
            )
        )
        return 0

    service = AuthorityService(config)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: service.request_stop())
    signal.signal(signal.SIGINT, lambda _signum, _frame: service.request_stop())
    try:
        service.start()
        service.serve_forever()
    except ServiceBoundaryError as error:
        print(f"authority service boundary error: {error}", file=sys.stderr)
        return 3
    except Exception:
        logging.getLogger("orchestra.authority_service").exception(
            "authority service failed"
        )
        return 4
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
