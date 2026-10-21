import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra.authority import (
    ExecutionAudience,
    Principal,
)
from orchestra.authority_client import AuthorityClient, AuthorityClientError
from orchestra.authority_service import (
    AuthorityService,
    PeerIdentity,
    ServiceBoundaryError,
    ServiceConfig,
    ServiceConfigurationError,
    SocketPathError,
    _request_digest,
    _validate_config_file,
    error_response,
)
from orchestra.contracts import (
    ArtifactReference,
    EvidenceReference,
    ExecutionReport,
    FileScope,
    IsolationRequirements,
    NetworkPolicy,
    ResourceLimits,
    TaskSpec,
    TestEvidence,
    ValidationOutcome,
    VerificationRecord,
    canonical_json,
)
from orchestra.peercred import PeerCredentials, peer_credentials


BASE_COMMIT = "a" * 40
BASE_TIME = datetime(2026, 10, 21, 0, 0, 0, tzinfo=timezone.utc)
ADAPTER = "fake-local-adapter"
RUNTIME = "local_process"
CLIENT_UIDS = {
    "coordinator": 24_201,
    "human": 24_202,
    "verifier": 24_203,
    "worker": 24_204,
    "operator": 24_205,
}
ROGUE_UID = 24_299


class MutableClock:
    def __init__(self, current=BASE_TIME):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current = self.current + timedelta(seconds=seconds)


class PeerSwitcher:
    """Test-only credential reader; the Linux boundary test uses real UIDs."""

    def __init__(self, uid=CLIENT_UIDS["coordinator"], gid=None):
        self.uid = uid
        self.gid = gid if gid is not None else os.getgid()

    def __call__(self, _sock):
        return PeerCredentials(self.uid, self.gid, None)


def make_task(**overrides):
    values = {
        "task_id": "task-1",
        "request_id": "request-1",
        "repository": "owner/repo",
        "base_commit": BASE_COMMIT,
        "agent_id": "fake-coding-agent",
        "model_provider": "fake-provider",
        "model_id": "fake-model",
        "coding_harness": "fake-harness",
        "permitted_runtimes": (RUNTIME,),
        "authorized_operations": (
            "read_repository", "edit_workspace", "run_tests",
        ),
        "file_scope": FileScope(read_paths=(".",), write_paths=("src",)),
        "network": NetworkPolicy("denied"),
        "isolation": IsolationRequirements(
            filesystem="ephemeral_workspace",
            credentials="broker_only",
            git_metadata="read_only",
            sandbox="ephemeral_runner",
        ),
        "resource_limits": ResourceLimits(max_runtime_seconds=120),
        "approval_class": "routine",
        "consequence_class": "routine_development",
        "instructions": "Authenticated service test task.",
    }
    values.update(overrides)
    return TaskSpec(**values)


def make_report(task, attempt_id, *, report_id="report-1", outcome="succeeded"):
    return ExecutionReport(
        report_id=report_id,
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=attempt_id,
        provider_run_id="fake-run-1",
        adapter_id=ADAPTER,
        runtime=RUNTIME,
        agent_id=task.agent_id,
        model_provider=task.model_provider,
        model_id=task.model_id,
        coding_harness=task.coding_harness,
        outcome=outcome,
        artifacts=(
            ArtifactReference(
                uri="artifact:result", sha256="b" * 64,
                media_type="text/plain", size_bytes=18,
            ),
        ),
        commit_sha=BASE_COMMIT,
        test_evidence=(
            TestEvidence("unit tests", "passed", "artifact:result"),
        ),
        started_at=BASE_TIME.isoformat(),
        finished_at=(BASE_TIME + timedelta(seconds=3)).isoformat(),
    )


def make_verification(task, attempt_id, clock, *, status="pass",
                      verification_id="verification-1"):
    passed = status == "pass"
    return VerificationRecord(
        verification_id=verification_id,
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=attempt_id,
        verifier_id=f"uid:{CLIENT_UIDS['verifier']}",
        method="independent-recompute",
        evidence_checked=(
            EvidenceReference("artifact", "artifact:result", "b" * 64),
        ),
        outcomes=(
            ValidationOutcome(
                "recomputed digest", "pass" if passed else "fail",
            ),
        ),
        status=status,
        verified_at=clock.current.isoformat(),
        failure_reasons=() if passed else ("digest mismatch",),
    )


def make_config(root: Path, *, clock=None, **overrides):
    state = root / "state"
    runtime = root / "run"
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    runtime.mkdir(parents=True, mode=0o750, exist_ok=True)
    task = make_task()
    values = {
        "authority_id": "authority:service-test",
        "service_uid": os.geteuid(),
        "database_path": str(state / "authority.sqlite3"),
        "socket_path": str(runtime / "authority.sock"),
        "socket_gid": os.getgid(),
        "peers": (
            PeerIdentity(CLIENT_UIDS["coordinator"], "coordinator", "coordinator"),
            PeerIdentity(CLIENT_UIDS["human"], "human", "approver"),
            PeerIdentity(CLIENT_UIDS["verifier"], "verifier", "verifier"),
            PeerIdentity(CLIENT_UIDS["worker"], "worker", "worker"),
            PeerIdentity(CLIENT_UIDS["operator"], "operator", "operator"),
        ),
        "audiences": (
            ExecutionAudience(
                adapter_id=ADAPTER,
                runtime=RUNTIME,
                agent_id=task.agent_id,
                model_provider=task.model_provider,
                model_id=task.model_id,
                coding_harness=task.coding_harness,
            ),
        ),
        "grant_ttl_seconds": 120,
        "approval_ttl_seconds": 300,
        "stale_attempt_seconds": 60,
        "max_message_bytes": 1_048_576,
        "socket_timeout_seconds": 5,
    }
    values.update(overrides)
    return ServiceConfig(**values)


class RoleClient(AuthorityClient):
    """Client that switches the test credential reader before every call."""

    def __init__(self, socket_path, switcher, uid):
        super().__init__(socket_path, timeout=5)
        self._switcher = switcher
        self._uid = uid

    def call(self, *args, **kwargs):
        self._switcher.uid = self._uid
        return super().call(*args, **kwargs)


class RunningService:
    def __init__(self, config, *, clock=None, switcher=None):
        self.config = config
        self.clock = clock or MutableClock()
        self.switcher = switcher or PeerSwitcher()
        self.service = AuthorityService(
            config, clock=self.clock, credential_reader=self.switcher,
        )
        self.thread = None

    def __enter__(self):
        self.service.start()
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self._wait_for_socket()
        return self

    def __exit__(self, *exc_info):
        self.service.stop()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
        return False

    def _wait_for_socket(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            if Path(self.config.socket_path).exists():
                return
            time.sleep(0.01)
        raise AssertionError("service socket did not appear")

    def client(self, role="coordinator"):
        return RoleClient(
            self.config.socket_path, self.switcher, CLIENT_UIDS[role],
        )

    def client_as_uid(self, uid):
        return RoleClient(self.config.socket_path, self.switcher, uid)


class PeerCredentialTests(unittest.TestCase):
    def test_kernel_peer_credentials_match_current_identity(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        credentials = peer_credentials(left)
        self.assertEqual(credentials.uid, os.getuid())
        self.assertEqual(credentials.gid, os.getgid())
        if os.uname().sysname == "Linux":
            self.assertEqual(credentials.pid, os.getpid())


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="authority-config-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_config_round_trip_and_policy_identity(self):
        config = make_config(self.root)
        parsed = ServiceConfig.from_dict(config.to_dict())
        self.assertEqual(parsed.config_digest, config.config_digest)
        policy = parsed.to_authority_policy()
        self.assertIn(f"uid:{CLIENT_UIDS['human']}", policy.trusted_humans)
        self.assertIn(f"uid:{CLIENT_UIDS['verifier']}", policy.trusted_verifiers)
        self.assertIn(f"uid:{CLIENT_UIDS['operator']}", policy.trusted_operators)
        self.assertNotIn(f"uid:{CLIENT_UIDS['coordinator']}", policy.trusted_humans)

    def test_duplicate_uid_service_uid_and_missing_roles_are_rejected(self):
        config = make_config(self.root)
        with self.assertRaises(ServiceConfigurationError):
            replace(
                config,
                peers=(
                    PeerIdentity(CLIENT_UIDS["coordinator"], "coordinator", "a"),
                    PeerIdentity(CLIENT_UIDS["coordinator"], "human", "b"),
                    PeerIdentity(CLIENT_UIDS["verifier"], "verifier", "c"),
                    PeerIdentity(CLIENT_UIDS["operator"], "operator", "d"),
                ),
            )
        with self.assertRaises(ServiceConfigurationError):
            replace(
                config,
                peers=(
                    PeerIdentity(os.geteuid(), "coordinator", "a"),
                    PeerIdentity(CLIENT_UIDS["human"], "human", "b"),
                    PeerIdentity(CLIENT_UIDS["verifier"], "verifier", "c"),
                    PeerIdentity(CLIENT_UIDS["operator"], "operator", "d"),
                ),
            )
        with self.assertRaises(ServiceConfigurationError):
            replace(
                config,
                peers=(
                    PeerIdentity(CLIENT_UIDS["coordinator"], "coordinator", "a"),
                    PeerIdentity(CLIENT_UIDS["human"], "human", "b"),
                    PeerIdentity(CLIENT_UIDS["verifier"], "verifier", "c"),
                ),
            )

    def test_config_file_must_not_be_group_or_other_writable(self):
        config = make_config(self.root)
        path = self.root / "authority.json"
        path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
        os.chmod(path, 0o644)
        _validate_config_file(path, config)
        os.chmod(path, 0o666)
        with self.assertRaises(ServiceConfigurationError):
            _validate_config_file(path, config)

    def test_unsafe_paths_and_socket_modes_are_rejected(self):
        config = make_config(self.root)
        with self.assertRaises(ServiceConfigurationError):
            replace(config, database_path="relative.db")
        with self.assertRaises(ServiceConfigurationError):
            replace(config, socket_mode=0o666)
        with self.assertRaises(ServiceConfigurationError):
            replace(config, service_uid=0)


class AuthorityServiceProtocolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="authority-service-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()
        self.config = make_config(self.root)
        self.running = RunningService(self.config, clock=self.clock)
        self.running.__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.coordinator = self.running.client("coordinator")
        self.human = self.running.client("human")
        self.verifier = self.running.client("verifier")
        self.worker = self.running.client("worker")
        self.operator = self.running.client("operator")
        self.task = make_task()

    def test_full_lifecycle_with_server_derived_roles(self):
        health = self.coordinator.health()
        self.assertEqual(health["peer_role"], "coordinator")
        self.assertEqual(health["peer_uid"], CLIENT_UIDS["coordinator"])

        proposal = self.coordinator.propose(self.task)
        self.assertEqual(proposal["proposal_digest"], self.task.digest)

        with self.assertRaises(AuthorityClientError) as caught:
            self.coordinator.request_execution(
                self.task, adapter_id=ADAPTER, runtime=RUNTIME,
                idempotency_key="execution-1",
            )
        self.assertEqual(caught.exception.code, "approval_required")

        with self.assertRaises(AuthorityClientError) as caught:
            self.coordinator.decide_approval(
                self.task.digest, decision="approved",
            )
        self.assertEqual(caught.exception.code, "operation_not_permitted")

        approval = self.human.decide_approval(
            self.task.digest, decision="approved", reason="approved over socket",
        )
        self.assertEqual(approval["source"], "human")

        issued = self.coordinator.request_execution(
            self.task, adapter_id=ADAPTER, runtime=RUNTIME,
            idempotency_key="execution-1",
        )
        attempt_id = issued["attempt"]["attempt_id"]
        grant = issued["grant"]
        self.assertEqual(issued["attempt"]["state"], "granted")

        consumed = self.coordinator.consume_grant(
            self.task, grant, adapter_id=ADAPTER, runtime=RUNTIME,
        )
        self.assertIsNotNone(consumed["consumed_at"])

        with self.assertRaises(AuthorityClientError):
            self.coordinator.record_dispatch(
                attempt_id, provider_run_id="fake-run-1",
                adapter_id=ADAPTER, runtime=RUNTIME,
            )

        self.worker.record_dispatch(
            attempt_id, provider_run_id="fake-run-1",
            adapter_id=ADAPTER, runtime=RUNTIME,
        )
        report = make_report(self.task, attempt_id)
        reported = self.worker.record_report(report)
        self.assertEqual(reported["state"], "reported")
        self.assertFalse(reported["verified"])

        with self.assertRaises(AuthorityClientError) as caught:
            self.worker.record_verification(
                make_verification(self.task, attempt_id, self.clock)
            )
        self.assertEqual(caught.exception.code, "operation_not_permitted")

        decision = self.verifier.record_verification(
            make_verification(self.task, attempt_id, self.clock)
        )
        self.assertTrue(decision["applied"])
        self.assertEqual(decision["final_state"], "verified")
        final = self.coordinator.attempt(attempt_id)
        self.assertTrue(final["verified"])

        verifications = self.coordinator.verifications(attempt_id)
        self.assertEqual(len(verifications), 1)
        self.assertEqual(verifications[0]["status"], "pass")

    def test_role_specific_operations_are_denied(self):
        for client, operation, payload in (
            (self.human, "propose", {"task": self.task.to_dict()}),
            (self.verifier, "consume_grant", {}),
            (self.worker, "decide_approval", {}),
            (self.operator, "record_report", {}),
            (self.operator, "request_execution", {}),
        ):
            with self.subTest(role=operation):
                with self.assertRaises(AuthorityClientError) as caught:
                    client.call(operation, payload)
                self.assertEqual(caught.exception.code, "operation_not_permitted")

    def test_identity_and_policy_fields_are_forbidden(self):
        for field, value in (
            ("role", "human"),
            ("principal", "uid:24202"),
            ("uid", CLIENT_UIDS["human"]),
            ("standing_authorizations", []),
        ):
            with self.subTest(field=field):
                with self.assertRaises(AuthorityClientError) as caught:
                    self.coordinator.call("propose", {
                        "task": self.task.to_dict(), field: value,
                    })
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertIn("forbidden", caught.exception.message)

    def test_unknown_operation_and_unauthorized_peer(self):
        with self.assertRaises(AuthorityClientError) as caught:
            self.coordinator.call("update_policy", {})
        self.assertEqual(caught.exception.code, "unknown_operation")

        rogue = self.running.client_as_uid(ROGUE_UID)
        with self.assertRaises(AuthorityClientError) as caught:
            rogue.health()
        self.assertEqual(caught.exception.code, "unauthorized_peer")


class RequestReplayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="authority-replay-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()
        self.config = make_config(self.root)
        self.running = RunningService(self.config, clock=self.clock)
        self.running.__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.coordinator = self.running.client("coordinator")
        self.task = make_task()

    def test_identical_replay_returns_cached_response(self):
        request_id = "replay-propose-1"
        first = self.coordinator.propose(self.task, request_id=request_id)
        second = self.coordinator.propose(self.task, request_id=request_id)
        self.assertEqual(first, second)

    def test_conflicting_replay_is_rejected(self):
        request_id = "replay-conflict-1"
        self.coordinator.propose(self.task, request_id=request_id)
        other = make_task(request_id="request-2")
        with self.assertRaises(AuthorityClientError) as caught:
            self.coordinator.propose(other, request_id=request_id)
        self.assertEqual(caught.exception.code, "request_id_conflict")

        with self.assertRaises(AuthorityClientError) as caught:
            self.coordinator.call("health", {}, request_id=request_id)
        self.assertEqual(caught.exception.code, "request_id_conflict")

    def test_in_progress_request_is_never_reexecuted_and_recovers_unknown(self):
        request_id = "interrupted-consume"
        payload = {"task": self.task.to_dict()}
        digest = _request_digest("consume_grant", payload)
        connection = sqlite3.connect(self.config.database_path)
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO rpc_requests(peer_uid, request_id, operation, "
            "request_digest, status, response_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'in_progress', NULL, ?, ?)",
            (CLIENT_UIDS["coordinator"], request_id, "consume_grant", digest,
             BASE_TIME.isoformat(), BASE_TIME.isoformat()),
        )
        connection.commit()

        with self.assertRaises(AuthorityClientError) as caught:
            self.coordinator.call("consume_grant", payload, request_id=request_id)
        self.assertEqual(caught.exception.code, "request_in_progress")

        self.running.__exit__(None, None, None)
        self.running = RunningService(self.config, clock=self.clock)
        self.running.__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        coordinator = self.running.client("coordinator")
        with self.assertRaises(AuthorityClientError) as caught:
            coordinator.call("consume_grant", payload, request_id=request_id)
        self.assertEqual(caught.exception.code, "request_outcome_unknown")
        status = coordinator.request_status(request_id)
        self.assertEqual(status["status"], "unknown")


class BoundaryAndSocketTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="authority-boundary-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_wrong_process_uid_is_refused(self):
        config = make_config(self.root)
        wrong = replace(config, service_uid=os.geteuid() + 1)
        service = AuthorityService(wrong)
        with self.assertRaises(ServiceBoundaryError):
            service.start()

    def test_state_and_runtime_directory_permissions_are_enforced(self):
        config = make_config(self.root)
        os.chmod(Path(config.database_path).parent, 0o755)
        with self.assertRaises(ServiceBoundaryError):
            AuthorityService(config).start()

        config2 = make_config(self.root / "second")
        os.chmod(Path(config2.socket_path).parent, 0o777)
        with self.assertRaises(ServiceBoundaryError):
            AuthorityService(config2).start()

    def test_non_socket_and_symlink_replacements_are_refused(self):
        config = make_config(self.root)
        socket_path = Path(config.socket_path)
        socket_path.write_text("not a socket", encoding="utf-8")
        with self.assertRaises(SocketPathError):
            AuthorityService(config).start()
        self.assertEqual(socket_path.read_text(encoding="utf-8"), "not a socket")
        socket_path.unlink()

        target = self.root / "target"
        target.write_text("target", encoding="utf-8")
        socket_path.symlink_to(target)
        with self.assertRaises(SocketPathError):
            AuthorityService(config).start()
        self.assertTrue(socket_path.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "target")

    def test_stale_socket_is_replaced_and_live_instance_is_refused(self):
        config = make_config(self.root)
        socket_path = Path(config.socket_path)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(socket_path))
        stale.close()
        self.assertTrue(socket_path.exists())

        clock = MutableClock()
        with RunningService(config, clock=clock) as first:
            with self.assertRaises(ServiceBoundaryError):
                second = AuthorityService(config, clock=clock)
                second.start()
            first.client("coordinator").health()
            self.assertEqual(
                Path(config.database_path).stat().st_mode & 0o777, 0o600,
            )
        self.assertFalse(socket_path.exists())
        self.assertFalse((Path(config.socket_path).parent / "authority.lock").exists())

    def test_service_audit_events_are_append_only(self):
        config = make_config(self.root)
        clock = MutableClock()
        with RunningService(config, clock=clock) as running:
            running.client("coordinator").health()
        connection = sqlite3.connect(config.database_path)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM service_events")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE service_events SET event='rewritten' WHERE sequence=1"
            )

    def test_restart_preserves_durable_state(self):
        config = make_config(self.root)
        clock = MutableClock()
        with RunningService(config, clock=clock) as first:
            client = first.client("coordinator")
            task = make_task()
            proposal = client.propose(task)
            first.__exit__(None, None, None)
        with RunningService(config, clock=clock) as second:
            client = second.client("coordinator")
            loaded = client.proposal(proposal["proposal_digest"])
            self.assertEqual(loaded["proposal_digest"], task.digest)
            events = second.service.service_events()
            self.assertTrue(any(e["event"] == "service_started" for e in events))


class ProtocolValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="authority-protocol-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = MutableClock()
        self.config = make_config(self.root)
        self.running = RunningService(self.config, clock=self.clock)
        self.running.__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.running.switcher.uid = CLIENT_UIDS["coordinator"]

    def _raw(self, payload: bytes) -> dict:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        self.addCleanup(connection.close)
        connection.connect(self.config.socket_path)
        connection.sendall(payload)
        data = b""
        while b"\n" not in data:
            chunk = connection.recv(8192)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))

    def test_malformed_json_and_envelope_are_rejected(self):
        response = self._raw(b"not-json\n")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_json")

        response = self._raw(b'{"version":1,"request_id":"x","operation":"health","payload":{},"uid":0}\n')
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")

        response = self._raw(
            b'{"version":2,"request_id":"x","operation":"health","payload":{}}\n'
        )
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_oversized_message_is_rejected(self):
        oversized = b"{" + b"x" * (self.config.max_message_bytes + 10) + b"}\n"
        response = self._raw(oversized)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_client_connection_error_is_reported(self):
        client = AuthorityClient(self.root / "missing.sock", timeout=1)
        with self.assertRaises(AuthorityClientError) as caught:
            client.health()
        self.assertEqual(caught.exception.code, "client_connection")


if __name__ == "__main__":
    unittest.main()
