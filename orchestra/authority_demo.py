"""Disposable end-to-end demonstration for the authority prototype.

Run with::

    python3 -m orchestra.authority_demo

The demo creates a temporary directory, a SQLite database and a fake workspace.
It performs the full contract -> proposal -> approval -> grant -> consumption ->
dispatch receipt -> untrusted report -> independent verification lifecycle.  It
uses no network, provider credential, production database or external worker.
"""

from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestra.authority import (
    ATTEMPT_VERIFIED,
    AuthorityPolicy,
    ExecutionAudience,
    Principal,
    PrototypeExecutionAuthority,
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
)


BASE_COMMIT = "a" * 40
ADAPTER_ID = "fake-local-adapter"


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="demo-task",
        request_id="demo-request",
        repository="owner/demo-repository",
        base_commit=BASE_COMMIT,
        agent_id="fake-coding-agent",
        model_provider="fake-provider",
        model_id="fake-model",
        coding_harness="fake-harness",
        permitted_runtimes=("local_process",),
        authorized_operations=(
            "read_repository", "edit_workspace", "run_tests",
        ),
        file_scope=FileScope(read_paths=(".",), write_paths=("src",)),
        network=NetworkPolicy("denied"),
        isolation=IsolationRequirements(
            filesystem="ephemeral_workspace",
            credentials="broker_only",
            git_metadata="read_only",
            sandbox="ephemeral_runner",
        ),
        resource_limits=ResourceLimits(max_runtime_seconds=120),
        approval_class="routine",
        consequence_class="routine_development",
        instructions="Write a disposable demonstration artifact.",
    )


def _policy() -> AuthorityPolicy:
    task = _task()
    return AuthorityPolicy(
        authority_id="authority:disposable-demo",
        trusted_humans=frozenset({"human:demo-owner"}),
        trusted_verifiers=frozenset({"verifier:demo-independent"}),
        trusted_operators=frozenset({"operator:demo-owner"}),
        audiences=(
            ExecutionAudience(
                adapter_id=ADAPTER_ID,
                runtime="local_process",
                agent_id=task.agent_id,
                model_provider=task.model_provider,
                model_id=task.model_id,
                coding_harness=task.coding_harness,
            ),
        ),
        standing_authorizations=(),
        grant_ttl_seconds=120,
        approval_ttl_seconds=300,
        stale_attempt_seconds=60,
    )


def _fake_worker(
    task: TaskSpec,
    grant: Any,
    workspace: Path,
    now: datetime,
) -> ExecutionReport:
    """A deterministic local fake worker with no provider or network access."""

    if grant.proposal_digest != task.digest:
        raise RuntimeError("fake worker received a grant for another proposal")
    target = workspace / "src" / "demo.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"orchestra authority prototype disposable artifact\n"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return ExecutionReport(
        report_id="report-demo-1",
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=grant.attempt_id,
        provider_run_id="fake-run-1",
        adapter_id=grant.adapter_id,
        runtime=grant.runtime,
        agent_id=task.agent_id,
        model_provider=task.model_provider,
        model_id=task.model_id,
        coding_harness=task.coding_harness,
        outcome="succeeded",
        artifacts=(
            ArtifactReference(
                uri="artifact:demo-result",
                sha256=digest,
                media_type="text/plain",
                size_bytes=len(payload),
            ),
        ),
        commit_sha=BASE_COMMIT,
        test_evidence=(
            TestEvidence(
                name="disposable demo check",
                status="passed",
                evidence_ref="artifact:demo-result",
            ),
        ),
        started_at=now.isoformat(),
        finished_at=now.isoformat(),
    )


def _independent_verifier(
    task: TaskSpec,
    report: ExecutionReport,
    workspace: Path,
    now: datetime,
) -> VerificationRecord:
    """Recompute artifact evidence; never trust the worker's success field."""

    artifact = report.artifacts[0]
    actual = hashlib.sha256(
        (workspace / "src" / "demo.txt").read_bytes()
    ).hexdigest()
    passed = actual == artifact.sha256 and report.outcome == "succeeded"
    return VerificationRecord(
        verification_id="verification-demo-1",
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=report.attempt_id,
        verifier_id="verifier:demo-independent",
        method="recomputed-artifact-digest",
        evidence_checked=(
            EvidenceReference(
                kind="artifact",
                uri=artifact.uri,
                sha256=actual,
            ),
        ),
        outcomes=(
            ValidationOutcome(
                claim="artifact digest matches the worker report",
                result="pass" if passed else "fail",
                detail=f"recomputed {actual}",
            ),
            ValidationOutcome(
                claim="worker outcome was successful",
                result="pass" if report.outcome == "succeeded" else "fail",
            ),
        ),
        status="pass" if passed else "fail",
        verified_at=now.isoformat(),
        failure_reasons=() if passed else ("artifact digest mismatch",),
    )


def run_demo(*, base_dir: str | Path | None = None, verbose: bool = True) -> dict[str, Any]:
    """Run the disposable lifecycle and return its durable result summary."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if base_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="orchestra-authority-demo-")
        root = Path(temporary.name)
    else:
        root = Path(base_dir)
        root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    clock = lambda: now.replace(microsecond=now.microsecond)
    task = _task()
    coordinator = Principal("coordinator:demo", "coordinator")
    human = Principal("human:demo-owner", "human")
    worker = Principal("worker:fake", "worker")
    verifier = Principal("verifier:demo-independent", "verifier")

    authority = PrototypeExecutionAuthority(
        root / "authority.sqlite3", _policy(), clock=clock,
    )
    try:
        proposal = authority.propose(task, proposer=coordinator)
        approval = authority.decide_approval(
            proposal.proposal_digest,
            decision="approved",
            approver=human,
            reason="disposable demonstration approval",
        )
        issued = authority.request_execution(
            task,
            adapter_id=ADAPTER_ID,
            runtime="local_process",
            idempotency_key="demo-execution-1",
            requester=coordinator,
        )
        consumed = authority.consume_grant(
            task,
            issued.grant,
            adapter_id=ADAPTER_ID,
            runtime="local_process",
            consumer=coordinator,
        )
        authority.record_dispatch(
            issued.attempt.attempt_id,
            provider_run_id="fake-run-1",
            adapter_id=ADAPTER_ID,
            runtime="local_process",
            worker=worker,
        )
        report = _fake_worker(task, issued.grant, workspace, now)
        reported = authority.record_report(report, worker=worker)

        # A successful worker report must not verify itself.
        assert reported.state == "reported"
        assert reported.verified is False
        assert report.completion_verified is False

        verification = _independent_verifier(task, report, workspace, now)
        decision = authority.record_verification(verification, verifier=verifier)
        final = authority.attempt(issued.attempt.attempt_id)
        assert decision.final_state == ATTEMPT_VERIFIED
        assert final.verified is True
        events = authority.events()
        summary = {
            "root": str(root),
            "proposal_digest": proposal.proposal_digest,
            "approval_source": approval.source,
            "grant_id": issued.grant.grant_id,
            "grant_consumed_at": consumed.consumed_at,
            "attempt_id": issued.attempt.attempt_id,
            "reported_state": reported.state,
            "final_state": final.state,
            "verified": final.verified,
            "verification_status": verification.status,
            "event_types": [event.event_type for event in events],
            "workspace": str(workspace),
        }
        if verbose:
            print("Orchestra execution-authority prototype demo (disposable)")
            for key, value in summary.items():
                print(f"  {key}: {value}")
        return summary
    finally:
        authority.close()
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    run_demo()
