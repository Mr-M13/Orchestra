"""Provider-independent disposable coding-worker adapter.

This module executes only the bundled fake harness.  It has no production
provider integration and cannot issue grants or decide approvals.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from orchestra.adapters import (
    AdapterStatus, CancellationResult, DispatchReceipt, PreflightResult,
    RecoveryResult,
)
from orchestra.contracts import (
    AgentManifest, ArtifactReference, CapabilityClaim, Concurrency,
    CostCharacteristics, ExecutionError, ExecutionGrant, ExecutionReport,
    GrantValidationError, IsolationRequirements, NetworkPolicy, ResourceLimits,
    ResourceUsage, TaskSpec,
)
from orchestra.authority import execution_grant_from_dict
from orchestra.process_identity import ProcessIdentity, capture_process_identity

ADAPTER_ID = "disposable-coding-worker"
RUNTIME = "local_process"
HARNESS_ID = "disposable-harness"
MAX_INSTRUCTION_BYTES = 65_536
JOB_RECORD_VERSION = 2
LEGACY_IDENTITY_DETAIL = (
    "legacy job record has no verifiable process identity; "
    "automatic recovery is forbidden"
)

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)


class AdapterExecutionError(RuntimeError):
    pass


class AuthorityUnavailableError(AdapterExecutionError):
    pass


@runtime_checkable
class ExecutionAuthorityClient(Protocol):
    """Authenticated Phase 3A operations required by this adapter."""

    def validate_grant(self, task: TaskSpec, grant: ExecutionGrant, *,
                       adapter_id: str, runtime: str,
                       request_id: str | None = None) -> dict[str, Any]: ...

    def consume_grant(self, task: TaskSpec, grant: ExecutionGrant, *,
                      adapter_id: str, runtime: str,
                      request_id: str | None = None) -> dict[str, Any]: ...

    def request_status(self, target_request_id: str, *,
                       request_id: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DisposableHarnessConfig:
    behavior: str = "success"
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.behavior not in {"success", "fail", "sleep", "interrupt"}:
            raise ValueError("unknown disposable harness behavior")
        if not isinstance(self.delay_seconds, (int, float)) or not 0 <= self.delay_seconds <= 300:
            raise ValueError("invalid disposable harness delay")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
    finally:
        try: os.close(descriptor)
        except OSError: pass
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_json(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AdapterExecutionError("adapter state is not a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AdapterExecutionError("adapter state is malformed")
    return value


class DisposableCodingWorkerAdapter:
    """One fixed local-process adapter for a credential-free fake harness."""

    def __init__(self, state_root: Path, authority: ExecutionAuthorityClient | None,
                 *, harness: DisposableHarnessConfig = DisposableHarnessConfig(),
                 clock: Any = None, fault_injector: Any = None) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        self.authority = authority
        self.harness = harness
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault_injector = fault_injector
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def describe(self) -> AgentManifest:
        return AgentManifest(
            agent_id="disposable-coding-agent", display_name="Disposable coding agent",
            agent_version="1.0", agent_class="coding", model_provider="fake-provider",
            model_ids=("fake-model",), coding_harness=HARNESS_ID,
            supported_runtimes=(RUNTIME,), capabilities=tuple(CapabilityClaim(name)
                for name in ("read_repository", "edit_workspace", "run_tests", "produce_patch")),
            isolation=IsolationRequirements(filesystem="ephemeral_workspace",
                credentials="broker_only", git_metadata="read_only", sandbox="ephemeral_runner"),
            network=NetworkPolicy("denied"), credential_refs=(),
            resource_limits=ResourceLimits(max_runtime_seconds=300, max_output_bytes=65_536),
            concurrency=Concurrency(max_parallel_attempts=4, exclusive_worker=False),
            cost=CostCharacteristics(cost_class="free"), verification_status="unverified",
            notes="Disposable test harness only; no Codex, DeepSeek, Claude or Gemini integration.",
        )

    def _validate(self, task: TaskSpec, grant: ExecutionGrant) -> tuple[str, ...]:
        checks = ["contract_binding", "fixed_audience", "authenticated_authority", "no_credentials"]
        manifest = self.describe()
        if task.agent_id != manifest.agent_id or task.model_provider != manifest.model_provider or task.model_id not in manifest.model_ids or task.coding_harness != manifest.coding_harness:
            raise GrantValidationError("adapter_manifest_audience_mismatch")
        if task.network.mode != "denied" or task.isolation.filesystem != "ephemeral_workspace" or task.isolation.credentials != "broker_only":
            raise GrantValidationError("disposable_isolation_policy_mismatch")
        if task.instructions is None or len(task.instructions.encode("utf-8")) > min(MAX_INSTRUCTION_BYTES, task.resource_limits.max_output_bytes):
            raise GrantValidationError("task_instructions_outside_disposable_bound")
        grant.validate_binding(task, attempt_id=grant.attempt_id,
            adapter_id=ADAPTER_ID, runtime=RUNTIME, now=self.clock(), consumed=False)
        if self.authority is None:
            raise AuthorityUnavailableError("authenticated execution authority is unavailable")
        try:
            self.authority.validate_grant(task, grant, adapter_id=ADAPTER_ID, runtime=RUNTIME)
        except AuthorityUnavailableError:
            raise
        except Exception as exc:
            raise AuthorityUnavailableError("execution authority validation failed") from exc
        return tuple(checks)

    def _fault(self, stage: str, record: dict[str, Any]) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage, dict(record))

    def preflight(self, task: TaskSpec, grant: ExecutionGrant) -> PreflightResult:
        try:
            return PreflightResult(True, checks=self._validate(task, grant))
        except Exception as exc:
            return PreflightResult(False, reasons=(type(exc).__name__,),
                checks=("fail_closed",))

    def _job_dir(self, attempt_id: str) -> Path:
        if not isinstance(attempt_id, str) or not attempt_id.startswith("attempt-") or not attempt_id.replace("-", "").isalnum():
            raise AdapterExecutionError("unsafe attempt id")
        return self.state_root / attempt_id

    @contextmanager
    def _attempt_lock(self, attempt_id: str):
        lock_path = self.state_root / ("." + attempt_id + ".lock")
        key = str(lock_path)
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.get(key)
            if thread_lock is None:
                thread_lock = threading.Lock()
                _THREAD_LOCKS[key] = thread_lock
        with thread_lock:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def dispatch(self, task: TaskSpec, grant: ExecutionGrant) -> DispatchReceipt:
        with self._attempt_lock(grant.attempt_id):
            return self._dispatch_locked(task, grant)

    def _dispatch_locked(self, task: TaskSpec,
                         grant: ExecutionGrant) -> DispatchReceipt:
        job = self._job_dir(grant.attempt_id)
        record_path = job / "job.json"
        if record_path.exists():
            record = _read_json(record_path)
            if record.get("proposal_digest") != task.digest or record.get("grant_id") != grant.grant_id:
                raise AdapterExecutionError("attempt id already names another execution")
            durable_grant = execution_grant_from_dict(_read_json(job / "grant.json"))
            if durable_grant.to_dict() != grant.to_dict():
                raise AdapterExecutionError("grant differs from durable execution intent")
            if record.get("version") != JOB_RECORD_VERSION:
                if record.get("state") not in {"intent_prepared", "consumed"}:
                    if record.get("state") != "unknown":
                        record["state"] = "unknown"
                        record["legacy_identity"] = True
                        _atomic_json(record_path, record)
                    raise AdapterExecutionError(LEGACY_IDENTITY_DETAIL)
                record["version"] = JOB_RECORD_VERSION
            if record.get("state") in {"intent_prepared", "consumed"}:
                recovery = self._recover_locked(task, grant.attempt_id)
                if recovery.state != "recovered":
                    raise AdapterExecutionError(recovery.detail or "execution recovery failed")
            elif record.get("state") == "launching":
                record["state"] = "unknown"; _atomic_json(record_path, record)
                raise AdapterExecutionError("launch outcome is unknown")
            elif record.get("state") == "unknown":
                raise AdapterExecutionError(
                    "execution outcome is unknown; automatic retry is forbidden"
                )
            return self._receipt(record)
        self._validate(task, grant)
        try:
            job.mkdir(mode=0o700)
        except FileExistsError:
            raise AdapterExecutionError("concurrent adapter intent exists") from None
        provider_run_id = "local-" + hashlib.sha256(grant.attempt_id.encode()).hexdigest()[:24]
        dispatched_at = _now()
        record = {"version": JOB_RECORD_VERSION, "attempt_id": grant.attempt_id,
            "provider_run_id": provider_run_id, "adapter_id": ADAPTER_ID, "runtime": RUNTIME,
            "proposal_digest": task.digest, "grant_id": grant.grant_id,
            "task_id": task.task_id, "state": "intent_prepared", "dispatched_at": dispatched_at,
            "consume_request_id": "consume-" + secrets.token_hex(16),
            "consume_receipt": None,
            "started_at": None, "deadline": time.time() + task.resource_limits.max_runtime_seconds,
            "pid": None, "process_identity": None,
            "max_output_bytes": task.resource_limits.max_output_bytes}
        _atomic_json(job / "grant.json", grant.to_dict())
        _atomic_json(record_path, record)
        self._fault("after_intent", record)
        record = self._consume(task, grant, record_path, record)
        self._fault("after_consume", record)
        return self._launch(task, grant, record_path, record)

    def _consume(self, task: TaskSpec, grant: ExecutionGrant, record_path: Path,
                 record: dict[str, Any]) -> dict[str, Any]:
        if self.authority is None:
            raise AuthorityUnavailableError("authenticated execution authority is unavailable")
        try:
            receipt = self.authority.consume_grant(task, grant, adapter_id=ADAPTER_ID,
                runtime=RUNTIME, request_id=record["consume_request_id"])
        except Exception as exc:
            raise AuthorityUnavailableError("atomic authority grant consumption failed") from exc
        self._validate_consume_receipt(task, grant, receipt)
        record["consume_receipt"] = receipt
        record["state"] = "consumed"
        _atomic_json(record_path, record)
        return record

    @staticmethod
    def _validate_consume_receipt(task: TaskSpec, grant: ExecutionGrant,
                                  receipt: Any) -> None:
        if (not isinstance(receipt, dict)
                or receipt.get("grant_id") != grant.grant_id
                or receipt.get("attempt_id") != grant.attempt_id
                or receipt.get("proposal_digest") != task.digest
                or not isinstance(receipt.get("consumed_at"), str)
                or not receipt.get("consumed_at")):
            raise AuthorityUnavailableError(
                "authority returned an invalid consume receipt"
            )

    def _launch(self, task: TaskSpec, grant: ExecutionGrant, record_path: Path,
                record: dict[str, Any]) -> DispatchReceipt:
        record["version"] = JOB_RECORD_VERSION
        job = record_path.parent
        workspace = job / "workspace"
        try:
            workspace.mkdir(mode=0o700)
        except FileExistsError:
            metadata = workspace.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AdapterExecutionError("workspace recovery path is unsafe")
            os.chmod(workspace, 0o700)
        self._fault("after_workspace", record)
        spec_path = job / "harness.json"; result_path = job / "result.json"
        _atomic_json(spec_path, {"version": 1, "attempt_id": grant.attempt_id,
            "task_id": task.task_id, "proposal_digest": task.digest,
            "instructions": task.instructions, "workspace": str(workspace),
            "result_path": str(result_path), "behavior": self.harness.behavior,
            "delay_seconds": self.harness.delay_seconds,
            "max_output_bytes": task.resource_limits.max_output_bytes})
        self._fault("after_spec", record)
        record["state"] = "launching"
        _atomic_json(record_path, record)
        self._fault("after_launch_intent", record)
        environment = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        process = subprocess.Popen([sys.executable, "-m", "orchestra.disposable_coding_harness",
            "--spec", str(spec_path)], cwd=job, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            close_fds=True)
        identity = self._capture_process_identity(process)
        record.update({"state": "running", "pid": process.pid,
            "process_identity": identity.to_dict() if identity is not None else None,
            "started_at": _now()})
        _atomic_json(record_path, record)
        self._processes[grant.attempt_id] = process
        self._fault("after_process_start", record)
        return self._receipt(record)

    @staticmethod
    def _capture_process_identity(process: subprocess.Popen[bytes]) -> ProcessIdentity | None:
        for _attempt in range(5):
            identity = capture_process_identity(
                process.pid, expected_uid=os.geteuid(),
            )
            if identity is not None:
                return identity
            if process.poll() is not None:
                return None
            time.sleep(0.01)
        return None

    @staticmethod
    def _identity(record: dict[str, Any]) -> ProcessIdentity | None:
        payload = record.get("process_identity")
        if not isinstance(payload, dict):
            return None
        try:
            return ProcessIdentity.from_dict(payload)
        except (TypeError, ValueError):
            return None

    def _receipt(self, record: dict[str, Any]) -> DispatchReceipt:
        return DispatchReceipt(record["attempt_id"], record["provider_run_id"],
            record["adapter_id"], record["runtime"], record["dispatched_at"])

    def _record_for(self, receipt: DispatchReceipt) -> tuple[Path, dict[str, Any]]:
        if receipt.adapter_id != ADAPTER_ID or receipt.runtime != RUNTIME:
            raise AdapterExecutionError("receipt audience mismatch")
        path = self._job_dir(receipt.attempt_id) / "job.json"
        record = _read_json(path)
        expected = self._receipt(record)
        if expected != receipt: raise AdapterExecutionError("receipt does not match durable job")
        return path, record

    def _alive(self, record: dict[str, Any]) -> bool:
        expected = self._identity(record)
        if expected is None:
            return False
        actual = capture_process_identity(
            expected.pid, expected_uid=expected.uid,
        )
        return actual is not None and actual == expected

    def status(self, receipt: DispatchReceipt) -> AdapterStatus:
        path, record = self._record_for(receipt); job = path.parent
        result_path = job / "result.json"
        if record.get("version") != JOB_RECORD_VERSION:
            record["state"] = "unknown"
            record["legacy_identity"] = True
            _atomic_json(path, record)
            return AdapterStatus(
                "unknown", provider_state="outcome_unknown",
                detail=LEGACY_IDENTITY_DETAIL,
            )
        if record.get("terminal_outcome") == "timed_out":
            return AdapterStatus("failed", provider_state="timed_out")
        if result_path.exists():
            result = _read_json(result_path); outcome = result.get("outcome")
            state = "succeeded" if outcome == "succeeded" else ("cancelled" if outcome == "cancelled" else "failed")
            record["state"] = state; _atomic_json(path, record)
            self._reap(receipt.attempt_id)
            return AdapterStatus(state, provider_state=outcome)
        if record["state"] in {"cancelled", "failed", "unknown"}:
            return AdapterStatus(record["state"], provider_state=record["state"])
        if time.time() >= record["deadline"]:
            if self._terminate(record):
                record["state"] = "failed"; record["terminal_outcome"] = "timed_out"
                _atomic_json(path, record)
                return AdapterStatus("failed", provider_state="timed_out")
            record["state"] = "unknown"; _atomic_json(path, record)
            return AdapterStatus("unknown", provider_state="outcome_unknown")
        process = self._processes.get(receipt.attempt_id)
        if process is not None and process.poll() is not None:
            record["state"] = "failed"; record["terminal_outcome"] = "interrupted"; _atomic_json(path, record)
            return AdapterStatus("failed", provider_state="interrupted")
        if not self._alive(record):
            record["state"] = "unknown"; _atomic_json(path, record)
            return AdapterStatus("unknown", provider_state="outcome_unknown")
        return AdapterStatus("running", provider_state="running")

    def _terminate(self, record: dict[str, Any]) -> bool:
        identity = self._identity(record)
        if identity is None or not self._alive(record):
            return False
        try:
            os.killpg(identity.pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + 2
        while self._alive(record) and time.monotonic() < deadline:
            self._reap(record["attempt_id"])
            time.sleep(0.01)
        if self._alive(record):
            try:
                os.killpg(identity.pgid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except OSError:
                return False
        self._reap(record["attempt_id"])
        process = self._processes.get(record["attempt_id"])
        return ((process is not None and process.poll() is not None)
                or self._process_group_absent(identity.pgid))

    @staticmethod
    def _process_group_absent(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def _reap(self, attempt_id: str) -> None:
        process = self._processes.get(attempt_id)
        if process is None:
            return
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            return

    def cancel(self, receipt: DispatchReceipt, reason: str) -> CancellationResult:
        if not isinstance(reason, str) or not reason.strip(): raise ValueError("cancellation reason required")
        path, record = self._record_for(receipt)
        current = self.status(receipt)
        if current.state in {"succeeded", "failed", "cancelled"}:
            return CancellationResult(False, provider_state=current.provider_state, detail="already terminal")
        requested = self._terminate(record)
        record["state"] = "cancelled" if requested else "unknown"
        record["cancellation_reason"] = reason[:1000]; _atomic_json(path, record)
        return CancellationResult(requested, provider_state=record["state"])

    def collect(self, receipt: DispatchReceipt) -> ExecutionReport:
        path, record = self._record_for(receipt); job = path.parent
        existing = job / "report.json"
        if existing.exists(): return self._report_from_json(_read_json(existing))
        current = self.status(receipt)
        if current.state in {"running", "queued"}: raise AdapterExecutionError("execution is not terminal")
        outcome = record.get("terminal_outcome") or ({"succeeded":"succeeded", "cancelled":"cancelled", "unknown":"unknown"}.get(current.state, "failed"))
        result_path = job / "result.json"; artifacts: tuple[ArtifactReference, ...] = ()
        started, finished = record.get("started_at"), _now()
        if result_path.exists() and record.get("terminal_outcome") != "timed_out":
            result = _read_json(result_path); outcome = result.get("outcome", outcome)
            started, finished = result.get("started_at", started), result.get("finished_at", finished)
            if outcome == "succeeded":
                try:
                    payload = self._read_artifact(job, record["max_output_bytes"])
                except (OSError, AdapterExecutionError):
                    payload = b""
                digest = hashlib.sha256(payload).hexdigest()
                if (not payload or digest != result.get("artifact_sha256")
                        or len(payload) != result.get("artifact_size")):
                    outcome = "failed"
                else:
                    artifacts = (ArtifactReference(uri=f"artifact:{receipt.attempt_id}/result.txt",
                        sha256=digest, media_type="text/plain", size_bytes=len(payload)),)
        report = ExecutionReport(report_id="report-" + receipt.provider_run_id,
            task_id=record["task_id"], proposal_digest=record["proposal_digest"],
            attempt_id=receipt.attempt_id, provider_run_id=receipt.provider_run_id,
            adapter_id=ADAPTER_ID, runtime=RUNTIME, agent_id="disposable-coding-agent",
            model_provider="fake-provider", model_id="fake-model", coding_harness=HARNESS_ID,
            outcome=outcome, artifacts=artifacts, errors=(() if outcome == "succeeded" else
                (ExecutionError(code=f"worker_{outcome}", message="Disposable worker did not complete successfully."),)),
            resource_usage=ResourceUsage(), started_at=started, finished_at=finished)
        _atomic_json(existing, report.to_dict())
        shutil.rmtree(job / "workspace", ignore_errors=True); (job / "harness.json").unlink(missing_ok=True)
        return report

    @staticmethod
    def _read_artifact(job: Path, maximum: int) -> bytes:
        workspace = job / "workspace"
        directory = workspace / "src"
        for path in (workspace, directory):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AdapterExecutionError("artifact directory is unsafe")
        artifact = directory / "result.txt"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(artifact, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise AdapterExecutionError("artifact is unsafe or oversized")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                payload = source.read(maximum + 1)
            if len(payload) > maximum:
                raise AdapterExecutionError("artifact exceeded its bound")
            return payload
        finally:
            os.close(descriptor)

    @staticmethod
    def _report_from_json(value: dict[str, Any]) -> ExecutionReport:
        return ExecutionReport(**{**value,
            "artifacts": tuple(ArtifactReference(**item) for item in value.get("artifacts", [])),
            "errors": tuple(ExecutionError(**item) for item in value.get("errors", [])),
            "resource_usage": ResourceUsage(**value.get("resource_usage", {})),
            "test_evidence": (),})

    def recover(self, task: TaskSpec, attempt_id: str) -> RecoveryResult:
        with self._attempt_lock(attempt_id):
            return self._recover_locked(task, attempt_id)

    def _recover_locked(self, task: TaskSpec, attempt_id: str) -> RecoveryResult:
        path = self._job_dir(attempt_id) / "job.json"
        if not path.exists(): return RecoveryResult("not_found", "no durable adapter job")
        record = _read_json(path)
        if record.get("proposal_digest") != task.digest: return RecoveryResult("failed", "proposal binding mismatch")
        state = record.get("state")
        if record.get("version") != JOB_RECORD_VERSION:
            if state not in {"intent_prepared", "consumed"}:
                if state != "unknown":
                    record["state"] = "unknown"
                    record["legacy_identity"] = True
                    _atomic_json(path, record)
                return RecoveryResult("ambiguous", LEGACY_IDENTITY_DETAIL)
            record["version"] = JOB_RECORD_VERSION
        if state in {"intent_prepared", "consumed"}:
            try:
                grant = execution_grant_from_dict(_read_json(path.parent / "grant.json"))
                if (grant.grant_id != record.get("grant_id")
                        or grant.attempt_id != record.get("attempt_id")):
                    return RecoveryResult("failed", "durable grant binding mismatch")
                if self.authority is None:
                    raise AuthorityUnavailableError(
                        "authenticated execution authority is unavailable"
                    )
                status = self.authority.request_status(
                    record["consume_request_id"],
                    request_id="status-" + secrets.token_hex(16),
                )
                if status.get("status") in {"unknown", "in_progress"}:
                    return RecoveryResult(
                        "ambiguous", "grant consumption outcome is unknown",
                    )
                if status.get("status") == "succeeded":
                    if status.get("operation") != "consume_grant":
                        return RecoveryResult(
                            "failed", "request was not grant consumption",
                        )
                    response = status.get("response") or {}
                    if response.get("ok") is not True:
                        return RecoveryResult(
                            "failed", "grant consumption was refused",
                        )
                    receipt = response.get("result")
                    self._validate_consume_receipt(task, grant, receipt)
                    if state == "intent_prepared":
                        record["consume_receipt"] = receipt; record["state"] = "consumed"
                        _atomic_json(path, record)
                    elif record.get("consume_receipt") != receipt:
                        return RecoveryResult(
                            "failed", "durable consume receipt mismatch",
                        )
                elif status.get("status") == "not_found":
                    if state == "intent_prepared":
                        record = self._consume(task, grant, path, record)
                    else:
                        return RecoveryResult(
                            "failed", "authority does not recognise consume receipt",
                        )
                else:
                    return RecoveryResult(
                        "failed", "grant consumption was refused",
                    )
                self._validate_consume_receipt(
                    task, grant, record.get("consume_receipt"),
                )
                self._launch(task, grant, path, record)
                return RecoveryResult("recovered", "running")
            except Exception as exc:
                return RecoveryResult("failed", type(exc).__name__)
        if state == "launching":
            record["state"] = "unknown"; _atomic_json(path, record)
            return RecoveryResult("ambiguous", "process launch outcome is unknown")
        receipt = self._receipt(record); state = self.status(receipt)
        if state.state == "unknown": return RecoveryResult("ambiguous", "external outcome is unknown; automatic retry is forbidden")
        return RecoveryResult("recovered", state.state)
