"""Provider-independent disposable coding-worker adapter.

This module executes only the bundled fake harness.  It has no production
provider integration and cannot issue grants or decide approvals.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
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

ADAPTER_ID = "disposable-coding-worker"
RUNTIME = "local_process"
HARNESS_ID = "disposable-harness"
MAX_INSTRUCTION_BYTES = 65_536


class AdapterExecutionError(RuntimeError):
    pass


class AuthorityUnavailableError(AdapterExecutionError):
    pass


@runtime_checkable
class ExecutionGrantValidator(Protocol):
    """Future Phase 3A boundary; implementations own authenticated transport."""

    def validate_grant(self, task: TaskSpec, grant: ExecutionGrant, *,
                       adapter_id: str, runtime: str) -> None:
        """Return only after authenticated authority validation; otherwise raise."""


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
    finally:
        try: os.close(descriptor)
        except OSError: pass
    temporary.replace(path)


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

    def __init__(self, state_root: Path, authority: ExecutionGrantValidator | None,
                 *, harness: DisposableHarnessConfig = DisposableHarnessConfig(),
                 clock: Any = None) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        self.authority = authority
        self.harness = harness
        self.clock = clock or (lambda: datetime.now(timezone.utc))
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
        checks = ["contract_binding", "fixed_audience", "authority_validation", "no_credentials"]
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

    def dispatch(self, task: TaskSpec, grant: ExecutionGrant) -> DispatchReceipt:
        checks = self._validate(task, grant)
        del checks
        job = self._job_dir(grant.attempt_id)
        record_path = job / "job.json"
        if record_path.exists():
            record = _read_json(record_path)
            if record.get("proposal_digest") != task.digest or record.get("grant_id") != grant.grant_id:
                raise AdapterExecutionError("attempt id already names another execution")
            return self._receipt(record)
        job.mkdir(mode=0o700)
        workspace = job / "workspace"; workspace.mkdir(mode=0o700)
        spec_path = job / "harness.json"; result_path = job / "result.json"
        provider_run_id = "local-" + hashlib.sha256(grant.attempt_id.encode()).hexdigest()[:24]
        dispatched_at = _now()
        record = {"version": 1, "attempt_id": grant.attempt_id,
            "provider_run_id": provider_run_id, "adapter_id": ADAPTER_ID, "runtime": RUNTIME,
            "proposal_digest": task.digest, "grant_id": grant.grant_id,
            "task_id": task.task_id, "state": "queued", "dispatched_at": dispatched_at,
            "started_at": None, "deadline": time.time() + task.resource_limits.max_runtime_seconds,
            "pid": None, "pid_start": None, "max_output_bytes": task.resource_limits.max_output_bytes}
        _atomic_json(record_path, record)
        _atomic_json(spec_path, {"version": 1, "attempt_id": grant.attempt_id,
            "task_id": task.task_id, "proposal_digest": task.digest,
            "instructions": task.instructions, "workspace": str(workspace),
            "result_path": str(result_path), "behavior": self.harness.behavior,
            "delay_seconds": self.harness.delay_seconds,
            "max_output_bytes": task.resource_limits.max_output_bytes})
        environment = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        process = subprocess.Popen([sys.executable, "-m", "orchestra.disposable_coding_harness",
            "--spec", str(spec_path)], cwd=job, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            close_fds=True)
        record.update({"state": "running", "pid": process.pid,
            "pid_start": self._pid_start(process.pid), "started_at": _now()})
        _atomic_json(record_path, record)
        self._processes[grant.attempt_id] = process
        return self._receipt(record)

    @staticmethod
    def _pid_start(pid: int) -> str | None:
        try: return Path(f"/proc/{pid}/stat").read_text().split()[21]
        except (OSError, IndexError): return None

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
        pid = record.get("pid")
        return isinstance(pid, int) and self._pid_start(pid) == record.get("pid_start")

    def status(self, receipt: DispatchReceipt) -> AdapterStatus:
        path, record = self._record_for(receipt); job = path.parent
        result_path = job / "result.json"
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
            self._terminate(record); record["state"] = "failed"; record["terminal_outcome"] = "timed_out"; _atomic_json(path, record)
            return AdapterStatus("failed", provider_state="timed_out")
        process = self._processes.get(receipt.attempt_id)
        if process is not None and process.poll() is not None:
            record["state"] = "failed"; record["terminal_outcome"] = "interrupted"; _atomic_json(path, record)
            return AdapterStatus("failed", provider_state="interrupted")
        if not self._alive(record):
            record["state"] = "unknown"; _atomic_json(path, record)
            return AdapterStatus("unknown", provider_state="outcome_unknown")
        return AdapterStatus("running", provider_state="running")

    def _terminate(self, record: dict[str, Any]) -> bool:
        if not self._alive(record): return False
        try: os.killpg(record["pid"], signal.SIGTERM)
        except ProcessLookupError: return False
        deadline = time.monotonic() + 2
        while self._alive(record) and time.monotonic() < deadline: time.sleep(0.01)
        if self._alive(record):
            try: os.killpg(record["pid"], signal.SIGKILL)
            except ProcessLookupError: pass
        self._reap(record["attempt_id"])
        return True

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
        path = self._job_dir(attempt_id) / "job.json"
        if not path.exists(): return RecoveryResult("not_found", "no durable adapter job")
        record = _read_json(path)
        if record.get("proposal_digest") != task.digest: return RecoveryResult("failed", "proposal binding mismatch")
        receipt = self._receipt(record); state = self.status(receipt)
        if state.state == "unknown": return RecoveryResult("ambiguous", "external outcome is unknown; automatic retry is forbidden")
        return RecoveryResult("recovered", state.state)
