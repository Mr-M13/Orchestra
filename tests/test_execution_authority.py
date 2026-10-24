import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra.authority import (
    ATTEMPT_CANCELLED,
    ATTEMPT_CANCELLED_UNCERTAIN,
    ATTEMPT_DISPATCHED,
    ATTEMPT_EXPIRED,
    ATTEMPT_FAILED_NO_SIDE_EFFECT,
    ATTEMPT_UNKNOWN,
    ATTEMPT_VERIFIED,
    ApprovalConflictError,
    ApprovalRequiredError,
    AttemptStateError,
    AudienceNotAllowedError,
    AuthorityConfigurationError,
    AuthorityPolicy,
    ExecutionAudience,
    GrantExpiredError,
    GrantReplayError,
    GrantRevokedError,
    GrantTamperedError,
    IdempotencyConflictError,
    Principal,
    ProposalConflictError,
    PrototypeExecutionAuthority,
    ReconciliationRequiredError,
    ReportConflictError,
    StandingAuthorization,
    UnauthorizedApprovalError,
    UnauthorizedPrincipalError,
    UnauthorizedReconciliationError,
    UnauthorizedVerifierError,
    UnsafeRetryError,
    VerificationConflictError,
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
from orchestra.authority_demo import run_demo


BASE_COMMIT = "a" * 40
BASE_TIME = datetime(2026, 10, 21, 0, 0, 0, tzinfo=timezone.utc)
ADAPTER = "fake-local-adapter"
RUNTIME = "local_process"


class MutableClock:
    def __init__(self, current=BASE_TIME):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current = self.current + timedelta(seconds=seconds)
        return self.current

    def iso(self):
        return self.current.isoformat()


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
        "instructions": "Implement the disposable test change.",
    }
    values.update(overrides)
    return TaskSpec(**values)


def default_audience(task=None):
    task = task or make_task()
    return ExecutionAudience(
        adapter_id=ADAPTER,
        runtime=RUNTIME,
        agent_id=task.agent_id,
        model_provider=task.model_provider,
        model_id=task.model_id,
        coding_harness=task.coding_harness,
    )


def default_policy(*, standing=(), audience=None, grant_ttl=300, approval_ttl=3600,
                   stale=60, authority_id="authority:test"):
    return AuthorityPolicy(
        authority_id=authority_id,
        trusted_humans=frozenset({"human:owner", "human:reviewer"}),
        trusted_verifiers=frozenset({"verifier:independent"}),
        trusted_operators=frozenset({"operator:owner"}),
        audiences=(audience or default_audience(),),
        standing_authorizations=tuple(standing),
        grant_ttl_seconds=grant_ttl,
        approval_ttl_seconds=approval_ttl,
        stale_attempt_seconds=stale,
    )


def standing_rule(task=None, **overrides):
    task = task or make_task()
    values = {
        "policy_id": "routine-repo-rule",
        "repository": task.repository,
        "agent_id": task.agent_id,
        "model_provider": task.model_provider,
        "model_id": task.model_id,
        "coding_harness": task.coding_harness,
        "adapter_id": ADAPTER,
        "runtime": RUNTIME,
        "allowed_operations": task.authorized_operations,
        "write_roots": ("src",),
        "max_runtime_seconds": 300,
    }
    values.update(overrides)
    return StandingAuthorization(**values)


COORDINATOR = Principal("coordinator:test", "coordinator")
HUMAN = Principal("human:owner", "human")
REVIEWER = Principal("human:reviewer", "human")
VERIFIER = Principal("verifier:independent", "verifier")
OPERATOR = Principal("operator:owner", "operator")
WORKER = Principal("worker:fake", "worker")


def make_report(task, attempt_id, *, report_id="report-1", outcome="succeeded",
                provider_run_id="fake-run-1", artifact_digest="b" * 64,
                artifact_uri="artifact:result"):
    return ExecutionReport(
        report_id=report_id,
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=attempt_id,
        provider_run_id=provider_run_id,
        adapter_id=ADAPTER,
        runtime=RUNTIME,
        agent_id=task.agent_id,
        model_provider=task.model_provider,
        model_id=task.model_id,
        coding_harness=task.coding_harness,
        outcome=outcome,
        artifacts=(
            ArtifactReference(
                uri=artifact_uri,
                sha256=artifact_digest,
                media_type="text/plain",
                size_bytes=18,
            ),
        ),
        commit_sha=BASE_COMMIT,
        test_evidence=(
            TestEvidence(
                name="unit tests", status="passed", evidence_ref=artifact_uri,
            ),
        ),
        started_at=BASE_TIME.isoformat(),
        finished_at=(BASE_TIME + timedelta(seconds=5)).isoformat(),
    )


def make_verification(task, attempt_id, clock, *, verification_id="verification-1",
                      status="pass", digest="b" * 64):
    passed = status == "pass"
    return VerificationRecord(
        verification_id=verification_id,
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=attempt_id,
        verifier_id=VERIFIER.principal_id,
        method="recomputed-artifact-digest",
        evidence_checked=(
            EvidenceReference("artifact", "artifact:result", digest),
        ),
        outcomes=(
            ValidationOutcome(
                "recomputed artifact digest",
                "pass" if passed else "fail",
                "independent check",
            ),
        ),
        status=status,
        verified_at=clock.iso(),
        failure_reasons=() if passed else ("independent check failed",),
    )


class AuthorityTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="orchestra-authority-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "authority.sqlite3"
        self.clock = MutableClock()
        self.policy = default_policy()
        self.authority = PrototypeExecutionAuthority(
            self.db, self.policy, clock=self.clock,
        )
        self.addCleanup(self.authority.close)

    def new_authority(self, policy=None):
        return PrototypeExecutionAuthority(
            self.db, policy or self.policy, clock=self.clock,
        )

    def propose(self, task=None, proposer=COORDINATOR):
        task = task or make_task()
        return self.authority.propose(task, proposer=proposer)

    def approve(self, task=None, decision="approved", approver=HUMAN):
        task = task or make_task()
        return self.authority.decide_approval(
            task.digest, decision=decision, approver=approver,
        )

    def issue(self, task=None, *, key="execution-1", adapter=ADAPTER,
              runtime=RUNTIME, requester=COORDINATOR):
        task = task or make_task()
        return self.authority.request_execution(
            task, adapter_id=adapter, runtime=runtime,
            idempotency_key=key, requester=requester,
        )

    def consume(self, task, issued, *, adapter=ADAPTER, runtime=RUNTIME):
        return self.authority.consume_grant(
            task, issued.grant, adapter_id=adapter, runtime=runtime,
            consumer=COORDINATOR,
        )

    def issue_consumed(self, task=None, *, key="execution-1"):
        task = task or make_task()
        self.propose(task)
        self.approve(task)
        issued = self.issue(task, key=key)
        self.consume(task, issued)
        return task, issued

    def report_and_dispatch(self, task, issued, **report_overrides):
        self.authority.record_dispatch(
            issued.attempt.attempt_id,
            provider_run_id=report_overrides.get("provider_run_id", "fake-run-1"),
            adapter_id=ADAPTER,
            runtime=RUNTIME,
            worker=WORKER,
        )
        report = make_report(task, issued.attempt.attempt_id, **report_overrides)
        return self.authority.record_report(report, worker=WORKER), report


class LifecycleTests(AuthorityTestBase):
    def test_full_lifecycle_and_durable_state(self):
        task = make_task()
        proposal = self.propose(task)
        self.assertEqual(proposal.proposal_digest, task.digest)

        approval = self.approve(task)
        self.assertEqual(approval.decision, "approved")
        self.assertEqual(approval.source, "human")

        issued = self.issue(task)
        self.assertEqual(issued.attempt.state, "granted")
        self.assertEqual(issued.grant.proposal_digest, task.digest)
        self.assertEqual(issued.grant.adapter_id, ADAPTER)
        self.assertEqual(issued.grant.runtime, RUNTIME)
        self.assertTrue(issued.grant.single_use)

        consumed = self.consume(task, issued)
        self.assertIsNotNone(consumed.consumed_at)
        self.assertEqual(self.authority.attempt(issued.attempt.attempt_id).state,
                         "dispatching")

        reported, report = self.report_and_dispatch(task, issued)
        self.assertEqual(reported.state, "reported")
        self.assertFalse(reported.verified)
        self.assertFalse(report.completion_verified)

        decision = self.authority.record_verification(
            make_verification(task, issued.attempt.attempt_id, self.clock),
            verifier=VERIFIER,
        )
        self.assertTrue(decision.applied)
        self.assertEqual(decision.final_state, ATTEMPT_VERIFIED)
        final = self.authority.attempt(issued.attempt.attempt_id)
        self.assertTrue(final.verified)

        # Reopen the database with the same owner policy: every record persists.
        reopened = self.new_authority()
        self.addCleanup(reopened.close)
        persisted = reopened.attempt(issued.attempt.attempt_id)
        self.assertEqual(persisted.state, ATTEMPT_VERIFIED)
        self.assertTrue(persisted.verified)
        self.assertEqual(reopened.proposal(task.digest).proposal_digest, task.digest)
        self.assertEqual(reopened.grant(issued.grant.grant_id).consumed_at,
                         consumed.consumed_at)
        self.assertGreaterEqual(len(reopened.events()), 7)

    def test_worker_success_is_never_automatic_verification(self):
        task, issued = self.issue_consumed()
        reported, report = self.report_and_dispatch(task, issued)
        self.assertEqual(reported.state, "reported")
        self.assertFalse(reported.verified)
        self.assertEqual(reported.report.trust, "untrusted_worker_report")
        self.assertFalse(report.completion_verified)
        self.assertEqual(self.authority.verification_records(
            issued.attempt.attempt_id
        ), ())

    def test_report_without_dispatch_receipt_is_rejected(self):
        task, issued = self.issue_consumed()
        with self.assertRaises(AttemptStateError):
            self.authority.record_report(
                make_report(task, issued.attempt.attempt_id), worker=WORKER,
            )
        self.assertEqual(self.authority.attempt(
            issued.attempt.attempt_id
        ).state, "dispatching")
        self.report_and_dispatch(task, issued)
        self.assertTrue(self.authority.attempt(
            issued.attempt.attempt_id
        ).report is not None)

    def test_demo_runs_full_lifecycle_in_a_temporary_directory(self):
        demo_root = self.root / "demo"
        summary = run_demo(base_dir=demo_root, verbose=False)
        self.assertTrue(summary["verified"])
        self.assertEqual(summary["final_state"], ATTEMPT_VERIFIED)
        self.assertEqual(summary["reported_state"], "reported")
        self.assertIn("untrusted_report_recorded", summary["event_types"])
        self.assertIn("verification_recorded", summary["event_types"])
        self.assertTrue((demo_root / "authority.sqlite3").exists())
        self.assertTrue((demo_root / "workspace" / "src" / "demo.txt").exists())


class ApprovalPolicyTests(AuthorityTestBase):
    def test_standing_authorisation_covers_only_exact_low_risk_task(self):
        policy = default_policy(standing=(standing_rule(),))
        authority = PrototypeExecutionAuthority(
            self.root / "standing.sqlite3", policy, clock=self.clock,
        )
        self.addCleanup(authority.close)
        task = make_task()
        authority.propose(task, proposer=COORDINATOR)
        issued = authority.request_execution(
            task, adapter_id=ADAPTER, runtime=RUNTIME,
            idempotency_key="standing-1", requester=COORDINATOR,
        )
        self.assertEqual(issued.approval.source, "standing")
        self.assertEqual(issued.approval.policy_id, "routine-repo-rule")
        self.assertEqual(issued.attempt.state, "granted")

    def test_sensitive_task_never_uses_standing_authorisation(self):
        policy = default_policy(standing=(standing_rule(),))
        authority = PrototypeExecutionAuthority(
            self.root / "sensitive.sqlite3", policy, clock=self.clock,
        )
        self.addCleanup(authority.close)
        task = make_task(
            task_id="task-sensitive",
            request_id="request-sensitive",
            authorized_operations=("deploy",),
            file_scope=FileScope(read_paths=("src",)),
            approval_class="sensitive",
            consequence_class="production_deployment",
        )
        authority.propose(task, proposer=COORDINATOR)
        with self.assertRaises(ApprovalRequiredError):
            authority.request_execution(
                task, adapter_id=ADAPTER, runtime=RUNTIME,
                idempotency_key="sensitive-1", requester=COORDINATOR,
            )
        # An explicit simulated human decision is still required.
        authority.decide_approval(
            task.digest, decision="approved", approver=HUMAN,
        )
        issued = authority.request_execution(
            task, adapter_id=ADAPTER, runtime=RUNTIME,
            idempotency_key="sensitive-2", requester=COORDINATOR,
        )
        self.assertEqual(issued.approval.source, "human")

    def test_standing_policy_rejects_sensitive_operations_at_configuration(self):
        with self.assertRaises(AuthorityConfigurationError):
            standing_rule(allowed_operations=("read_repository", "deploy"))

    def test_coordinator_cannot_approve_or_self_authorise(self):
        task = make_task()
        self.propose(task)
        with self.assertRaises(UnauthorizedApprovalError):
            self.authority.decide_approval(
                task.digest, decision="approved", approver=COORDINATOR,
            )
        with self.assertRaises(ApprovalRequiredError):
            self.issue(task)

    def test_coordinator_impersonation_is_rejected(self):
        task = make_task()
        self.propose(task)
        # Knowing the owner's identifier is not enough: the asserted role matters.
        impersonator = Principal("human:owner", "coordinator")
        with self.assertRaises(UnauthorizedApprovalError):
            self.authority.decide_approval(
                task.digest, decision="approved", approver=impersonator,
            )
        # Claiming the human role without a trusted identity also fails.
        outsider = Principal("coordinator:test", "human")
        with self.assertRaises(UnauthorizedApprovalError):
            self.authority.decide_approval(
                task.digest, decision="approved", approver=outsider,
            )

    def test_policy_cannot_be_widened_by_reopening_the_database(self):
        task = make_task()
        self.propose(task)
        with self.assertRaises(FrozenInstanceError):
            self.policy.grant_ttl_seconds = 9999
        widened = default_policy(grant_ttl=900)
        with self.assertRaises(AuthorityConfigurationError):
            PrototypeExecutionAuthority(self.db, widened, clock=self.clock)

    def test_approval_expiry_and_contradictory_decisions(self):
        task = make_task()
        self.propose(task)
        self.approve(task)
        with self.assertRaises(ApprovalConflictError):
            self.approve(task, decision="denied", approver=REVIEWER)

        clock = MutableClock()
        db = self.root / "approval-expiry.sqlite3"
        authority = PrototypeExecutionAuthority(db, self.policy, clock=clock)
        self.addCleanup(authority.close)
        task2 = make_task(task_id="task-2", request_id="request-2")
        authority.propose(task2, proposer=COORDINATOR)
        authority.decide_approval(task2.digest, decision="approved", approver=HUMAN)
        clock.advance(3601)
        with self.assertRaises(ApprovalRequiredError):
            authority.request_execution(
                task2, adapter_id=ADAPTER, runtime=RUNTIME,
                idempotency_key="expired-approval", requester=COORDINATOR,
            )


class GrantEnforcementTests(AuthorityTestBase):
    def test_modified_task_after_approval_is_rejected(self):
        task = make_task()
        self.propose(task)
        self.approve(task)
        altered = make_task(
            file_scope=FileScope(
                read_paths=(".",), write_paths=("src", "tests", "secrets"),
            ),
        )
        self.assertNotEqual(task.digest, altered.digest)
        with self.assertRaises(ProposalConflictError):
            self.issue(altered)
        with self.assertRaises(ProposalConflictError):
            self.propose(altered)

    def test_approval_does_not_transfer_to_a_changed_candidate(self):
        original = make_task()
        self.propose(original)
        self.approve(original)
        changed = make_task(
            request_id="request-changed",
            instructions="A changed candidate requires fresh approval.",
        )
        self.propose(changed)
        with self.assertRaises(ApprovalRequiredError):
            self.issue(changed, key="changed")

    def test_expired_grant_is_rejected_and_swept(self):
        task, issued = self.issue_consumed()
        # Build a fresh grant that is not consumed yet.
        second = make_task(task_id="task-exp", request_id="request-exp")
        self.propose(second)
        self.approve(second)
        issued = self.issue(second, key="expiring")
        self.clock.advance(301)
        with self.assertRaises(GrantExpiredError):
            self.consume(second, issued)
        recovery = self.authority.sweep()
        self.assertIn(issued.grant.grant_id, recovery.expired_grants)
        self.assertIn(issued.attempt.attempt_id, recovery.expired_attempts)
        state = self.authority.attempt(issued.attempt.attempt_id)
        self.assertEqual(state.state, ATTEMPT_EXPIRED)
        self.assertTrue(self.authority.grant(issued.grant.grant_id).revoked)

    def test_replayed_grant_is_rejected(self):
        task, issued = self.issue_consumed()
        with self.assertRaises(GrantReplayError):
            self.consume(task, issued)
        with self.assertRaises(GrantReplayError):
            self.authority.validate_grant(
                task, issued.grant, adapter_id=ADAPTER, runtime=RUNTIME,
            )

    def test_tampered_and_forged_grants_are_rejected(self):
        task, issued = self.issue_consumed()
        nonce_tampered = replace(issued.grant, nonce="m" * 64)
        with self.assertRaises(GrantTamperedError):
            self.authority.consume_grant(
                task, nonce_tampered, adapter_id=ADAPTER, runtime=RUNTIME,
                consumer=COORDINATOR,
            )

        proposal_tampered = replace(issued.grant, proposal_digest="c" * 64)
        with self.assertRaises(GrantTamperedError):
            self.authority.consume_grant(
                task, proposal_tampered, adapter_id=ADAPTER, runtime=RUNTIME,
                consumer=COORDINATOR,
            )

        forged = replace(issued.grant, grant_id="grant-forged", nonce="z" * 64)
        with self.assertRaises(Exception) as caught:
            self.authority.consume_grant(
                task, forged, adapter_id=ADAPTER, runtime=RUNTIME,
                consumer=COORDINATOR,
            )
        self.assertIn("not issued", str(caught.exception))

    def test_wrong_adapter_runtime_and_agent_audience_are_rejected(self):
        task = make_task()
        self.propose(task)
        self.approve(task)
        with self.assertRaises(AudienceNotAllowedError):
            self.issue(task, adapter="another-adapter")
        with self.assertRaises(AudienceNotAllowedError):
            self.issue(task, runtime="github_actions")

        wrong_model = make_task(
            task_id="task-model", request_id="request-model",
            model_id="different-model",
        )
        self.propose(wrong_model)
        with self.assertRaises(AudienceNotAllowedError):
            self.issue(wrong_model, key="wrong-model")

        issued = self.issue(task, key="audience-consume")
        with self.assertRaises(Exception):
            self.consume(task, issued, adapter="another-adapter")

    def test_report_and_dispatch_audience_must_match(self):
        task, issued = self.issue_consumed()
        with self.assertRaises(Exception):
            self.authority.record_dispatch(
                issued.attempt.attempt_id,
                provider_run_id="fake-run-1",
                adapter_id="other-adapter",
                runtime=RUNTIME,
                worker=WORKER,
            )
        self.authority.record_dispatch(
            issued.attempt.attempt_id,
            provider_run_id="fake-run-1",
            adapter_id=ADAPTER,
            runtime=RUNTIME,
            worker=WORKER,
        )
        forged = make_report(task, issued.attempt.attempt_id)
        forged = replace(forged, adapter_id="other-adapter")
        with self.assertRaises(Exception):
            self.authority.record_report(forged, worker=WORKER)

    def test_duplicate_and_conflicting_requests(self):
        task = make_task()
        first = self.propose(task)
        second = self.propose(task)
        self.assertEqual(first.proposal_digest, second.proposal_digest)
        with self.assertRaises(ProposalConflictError):
            self.propose(make_task(instructions="A conflicting request."))

        self.approve(task)
        issued = self.issue(task, key="idem-1")
        again = self.issue(task, key="idem-1")
        self.assertEqual(issued.grant.grant_id, again.grant.grant_id)
        self.assertEqual(issued.attempt.attempt_id, again.attempt.attempt_id)

        with self.assertRaises(IdempotencyConflictError):
            self.issue(task, key="idem-1", adapter="another-adapter")

    def test_atomic_single_use_under_concurrent_consumers(self):
        task = make_task()
        self.propose(task)
        self.approve(task)
        issued = self.issue(task, key="race")

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def consume_once():
            authority = PrototypeExecutionAuthority(
                self.db, self.policy, clock=self.clock,
            )
            try:
                barrier.wait(timeout=5)
                results.append(authority.consume_grant(
                    task, issued.grant, adapter_id=ADAPTER, runtime=RUNTIME,
                    consumer=COORDINATOR,
                ))
            except Exception as error:  # noqa: BLE001 - asserted below
                errors.append(error)
            finally:
                authority.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume_once) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], GrantReplayError)

    def test_interrupted_consumption_transaction_rolls_back(self):
        task = make_task()
        self.propose(task)
        self.approve(task)
        issued = self.issue(task, key="interrupted")

        class CrashingAuthority(PrototypeExecutionAuthority):
            def _after_consume_hook(self, connection, grant_id, attempt_id):
                raise RuntimeError("simulated crash before transaction commit")

        crashing = CrashingAuthority(self.db, self.policy, clock=self.clock)
        self.addCleanup(crashing.close)
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            crashing.consume_grant(
                task, issued.grant, adapter_id=ADAPTER, runtime=RUNTIME,
                consumer=COORDINATOR,
            )

        # No partial consumption survived the rollback: exactly one use remains.
        self.assertIsNone(self.authority.grant(issued.grant.grant_id).consumed_at)
        self.assertEqual(self.authority.attempt(issued.attempt.attempt_id).state,
                         "granted")
        consumed = self.consume(task, issued)
        self.assertIsNotNone(consumed.consumed_at)
        with self.assertRaises(GrantReplayError):
            self.consume(task, issued)


class VerificationTests(AuthorityTestBase):
    def test_false_worker_success_is_rejected_by_independent_verification(self):
        task, issued = self.issue_consumed()
        reported, _report = self.report_and_dispatch(task, issued)
        self.assertFalse(reported.verified)
        decision = self.authority.record_verification(
            make_verification(
                task, issued.attempt.attempt_id, self.clock, status="fail",
            ),
            verifier=VERIFIER,
        )
        self.assertTrue(decision.applied)
        self.assertEqual(decision.final_state, "rejected")
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)

    def test_worker_and_untrusted_verifier_cannot_verify(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        record = make_verification(task, issued.attempt.attempt_id, self.clock)
        with self.assertRaises(UnauthorizedVerifierError):
            self.authority.record_verification(record, verifier=WORKER)
        with self.assertRaises(UnauthorizedVerifierError):
            self.authority.record_verification(
                record, verifier=Principal("human:owner", "coordinator"),
            )
        with self.assertRaises(UnauthorizedVerifierError):
            self.authority.record_verification(
                record, verifier=Principal("verifier:unknown", "verifier"),
            )

    def test_verification_before_report_is_fail_closed(self):
        task, issued = self.issue_consumed()
        decision = self.authority.record_verification(
            make_verification(task, issued.attempt.attempt_id, self.clock),
            verifier=VERIFIER,
        )
        self.assertFalse(decision.applied)
        self.assertEqual(decision.reason, "report_missing")
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)
        self.assertEqual(len(self.authority.verification_records(
            issued.attempt.attempt_id
        )), 1)

    def test_contradictory_verification_evidence_fails_closed(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        passed = make_verification(
            task, issued.attempt.attempt_id, self.clock,
            verification_id="verification-pass",
        )
        first = self.authority.record_verification(passed, verifier=VERIFIER)
        self.assertEqual(first.final_state, ATTEMPT_VERIFIED)

        failed = make_verification(
            task, issued.attempt.attempt_id, self.clock,
            verification_id="verification-fail", status="fail",
        )
        second = self.authority.record_verification(failed, verifier=VERIFIER)
        self.assertEqual(second.final_state, "rejected")
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)
        self.assertEqual(len(self.authority.verification_records(
            issued.attempt.attempt_id
        )), 2)

        # A later pass cannot silently upgrade a rejected result.
        third = self.authority.record_verification(
            make_verification(
                task, issued.attempt.attempt_id, self.clock,
                verification_id="verification-pass-2",
            ),
            verifier=VERIFIER,
        )
        self.assertFalse(third.applied)
        self.assertEqual(third.reason, "contradictory_evidence_cannot_upgrade")
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)

    def test_duplicate_and_conflicting_verification_records(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        record = make_verification(task, issued.attempt.attempt_id, self.clock)
        first = self.authority.record_verification(record, verifier=VERIFIER)
        replay = self.authority.record_verification(record, verifier=VERIFIER)
        self.assertTrue(first.applied)
        self.assertTrue(replay.applied)
        self.assertEqual(replay.reason, "idempotent_replay")

        with self.assertRaises(VerificationConflictError):
            self.authority.record_verification(
                replace(record, status="fail", outcomes=(
                    ValidationOutcome("recomputed artifact digest", "fail"),
                ), failure_reasons=("mismatch",)),
                verifier=VERIFIER,
            )

    def test_verification_binding_mismatch_is_rejected(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        record = make_verification(task, issued.attempt.attempt_id, self.clock)
        wrong = replace(record, proposal_digest="d" * 64)
        with self.assertRaises(Exception):
            self.authority.record_verification(wrong, verifier=VERIFIER)
        wrong_identity = replace(record, verifier_id="verifier:somebody-else")
        with self.assertRaises(Exception):
            self.authority.record_verification(wrong_identity, verifier=VERIFIER)
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)


class CancellationRecoveryTests(AuthorityTestBase):
    def test_cancellation_before_consumption_revokes_grant_safely(self):
        task = make_task()
        self.propose(task)
        self.approve(task)
        issued = self.issue(task, key="cancel-before")
        result = self.authority.cancel_attempt(
            issued.attempt.attempt_id,
            principal=COORDINATOR,
            reason="owner cancelled before execution",
            idempotency_key="cancel-key-1",
        )
        self.assertTrue(result.applied)
        self.assertEqual(result.final_state, ATTEMPT_CANCELLED)
        self.assertTrue(self.authority.grant(issued.grant.grant_id).revoked)
        with self.assertRaises(GrantRevokedError):
            self.consume(task, issued)

        replay = self.authority.cancel_attempt(
            issued.attempt.attempt_id,
            principal=COORDINATOR,
            reason="owner cancelled before execution",
            idempotency_key="cancel-key-1",
        )
        self.assertEqual(replay.reason, "idempotent_replay")
        self.assertEqual(replay.cancellation_id, result.cancellation_id)

        # A provably unexecuted attempt may be replaced; the old grant stays dead.
        replacement = self.issue(task, key="cancel-before-retry")
        self.assertNotEqual(replacement.grant.grant_id, issued.grant.grant_id)

    def test_cancellation_after_consumption_is_uncertain_and_unverifiable(self):
        task, issued = self.issue_consumed()
        result = self.authority.cancel_attempt(
            issued.attempt.attempt_id,
            principal=COORDINATOR,
            reason="owner cancelled after dispatch",
        )
        self.assertTrue(result.applied)
        self.assertEqual(result.final_state, ATTEMPT_CANCELLED_UNCERTAIN)
        self.assertEqual(result.reason, "external_outcome_uncertain")

        # A late worker report is recorded as evidence but cannot verify the task.
        report = make_report(task, issued.attempt.attempt_id)
        self.authority.record_report(report, worker=WORKER)
        decision = self.authority.record_verification(
            make_verification(task, issued.attempt.attempt_id, self.clock),
            verifier=VERIFIER,
        )
        self.assertFalse(decision.applied)
        self.assertEqual(decision.final_state, ATTEMPT_CANCELLED_UNCERTAIN)
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)

        # Reconciliation with proof of no side effect makes a retry safe.
        # A stored report contradicts a no-side-effect claim, so the authority
        # refuses it rather than unlocking a retry.
        with self.assertRaises(ReconciliationRequiredError):
            self.authority.reconcile_unknown(
                issued.attempt.attempt_id,
                operator=OPERATOR,
                resolution="no_side_effect",
                reason="provider log claims the worker never started",
                evidence_ref="artifact:provider-log",
            )
        with self.assertRaises(UnsafeRetryError):
            self.issue(task, key="after-cancel-uncertain")

    def test_cancellation_race_with_report_is_fail_closed(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        result = self.authority.cancel_attempt(
            issued.attempt.attempt_id,
            principal=HUMAN,
            reason="owner cancelled after the report arrived",
        )
        self.assertEqual(result.final_state, ATTEMPT_CANCELLED_UNCERTAIN)
        decision = self.authority.record_verification(
            make_verification(task, issued.attempt.attempt_id, self.clock),
            verifier=VERIFIER,
        )
        self.assertFalse(decision.applied)
        self.assertFalse(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)

    def test_cancellation_after_verification_is_idempotent_terminal(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        self.authority.record_verification(
            make_verification(task, issued.attempt.attempt_id, self.clock),
            verifier=VERIFIER,
        )
        result = self.authority.cancel_attempt(
            issued.attempt.attempt_id,
            principal=COORDINATOR,
            reason="too late",
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.final_state, ATTEMPT_VERIFIED)
        self.assertEqual(result.reason, "already_terminal")
        self.assertTrue(self.authority.attempt(
            issued.attempt.attempt_id
        ).verified)

    def test_crash_restart_detects_stale_attempt_as_unknown(self):
        task, issued = self.issue_consumed()
        self.authority.record_dispatch(
            issued.attempt.attempt_id,
            provider_run_id="fake-run-1",
            adapter_id=ADAPTER,
            runtime=RUNTIME,
            worker=WORKER,
        )
        # Simulate process death after consumption and dispatch receipt.
        reopened = self.new_authority()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.attempt(issued.attempt.attempt_id).state,
                         ATTEMPT_DISPATCHED)
        self.clock.advance(61)
        recovery = reopened.recover_after_restart(stale_after_seconds=60)
        self.assertIn(issued.attempt.attempt_id, recovery.unknown_attempts)
        recovered = reopened.attempt(issued.attempt.attempt_id)
        self.assertEqual(recovered.state, ATTEMPT_UNKNOWN)
        self.assertFalse(recovered.verified)

    def test_unknown_outcome_blocks_blind_retry_and_requires_reconciliation(self):
        task, issued = self.issue_consumed()
        self.clock.advance(61)
        self.authority.sweep(stale_after_seconds=60)
        self.assertEqual(self.authority.attempt(
            issued.attempt.attempt_id
        ).state, ATTEMPT_UNKNOWN)

        # The same idempotent request observes the unknown attempt; it does not
        # mint a second grant.
        same = self.issue(task, key="execution-1")
        self.assertEqual(same.attempt.attempt_id, issued.attempt.attempt_id)
        self.assertEqual(same.attempt.state, ATTEMPT_UNKNOWN)
        with self.assertRaises(UnsafeRetryError):
            self.issue(task, key="blind-retry-1")
        with self.assertRaises(ReconciliationRequiredError):
            self.authority.record_report(
                make_report(task, issued.attempt.attempt_id), worker=WORKER,
            )

        # A coordinator cannot declare the outcome safe; only an operator can.
        with self.assertRaises(UnauthorizedReconciliationError):
            self.authority.reconcile_unknown(
                issued.attempt.attempt_id,
                operator=COORDINATOR,
                resolution="no_side_effect",
                reason="trust me",
                evidence_ref="artifact:log",
            )
        with self.assertRaises(ReconciliationRequiredError):
            self.authority.reconcile_unknown(
                issued.attempt.attempt_id,
                operator=OPERATOR,
                resolution="no_side_effect",
                reason="provider log proves no execution",
            )

        reconciled = self.authority.reconcile_unknown(
            issued.attempt.attempt_id,
            operator=OPERATOR,
            resolution="no_side_effect",
            reason="provider log proves no execution",
            evidence_ref="artifact:provider-log",
        )
        self.assertEqual(reconciled.state, ATTEMPT_FAILED_NO_SIDE_EFFECT)
        # Only after evidence-backed reconciliation is a new attempt allowed.
        replacement = self.issue(task, key="safe-retry-after-reconcile")
        self.assertNotEqual(replacement.attempt.attempt_id,
                            issued.attempt.attempt_id)

    def test_unknown_outcome_can_be_reconciled_as_executed_with_report(self):
        task, issued = self.issue_consumed()
        self.clock.advance(61)
        self.authority.sweep(stale_after_seconds=60)
        report = make_report(task, issued.attempt.attempt_id)
        reconciled = self.authority.reconcile_unknown(
            issued.attempt.attempt_id,
            operator=OPERATOR,
            resolution="executed",
            reason="provider run completed after the local crash",
            report=report,
        )
        self.assertEqual(reconciled.state, "reported")
        decision = self.authority.record_verification(
            make_verification(task, issued.attempt.attempt_id, self.clock),
            verifier=VERIFIER,
        )
        self.assertTrue(decision.applied)
        self.assertEqual(decision.final_state, ATTEMPT_VERIFIED)

    def test_still_unknown_reconciliation_does_not_unlock_retry(self):
        task, issued = self.issue_consumed()
        self.clock.advance(61)
        self.authority.sweep(stale_after_seconds=60)
        reconciled = self.authority.reconcile_unknown(
            issued.attempt.attempt_id,
            operator=OPERATOR,
            resolution="still_unknown",
            reason="provider status endpoint is unavailable",
        )
        self.assertEqual(reconciled.state, ATTEMPT_UNKNOWN)
        with self.assertRaises(UnsafeRetryError):
            self.issue(task, key="still-unknown-retry")

    def test_cancellation_of_unknown_outcome_remains_unknown(self):
        task, issued = self.issue_consumed()
        self.clock.advance(61)
        self.authority.sweep(stale_after_seconds=60)
        result = self.authority.cancel_attempt(
            issued.attempt.attempt_id,
            principal=OPERATOR,
            reason="owner requests cancellation while outcome is unknown",
        )
        self.assertTrue(result.applied)
        self.assertEqual(result.final_state, ATTEMPT_UNKNOWN)
        self.assertEqual(result.reason, "unknown_outcome_requires_reconciliation")


class AuditAndReportingTests(AuthorityTestBase):
    def test_audit_events_are_append_only(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        before = len(self.authority.events())
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM events")
        connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE events SET event_type = 'rewritten' WHERE sequence = 1"
            )
        connection.rollback()
        self.assertEqual(len(self.authority.events()), before)

    def test_no_credential_values_are_stored_in_audit_details(self):
        task, issued = self.issue_consumed()
        self.report_and_dispatch(task, issued)
        serialised = " ".join(
            str(event.to_dict()) for event in self.authority.events()
        )
        self.assertNotIn("api_key", serialised)
        self.assertNotIn("password", serialised)
        self.assertNotIn("token", serialised.lower())


if __name__ == "__main__":
    unittest.main()
