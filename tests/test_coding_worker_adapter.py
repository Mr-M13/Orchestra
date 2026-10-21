import json
import os
import signal
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestra.adapters import AgentAdapter
from orchestra.coding_worker_adapter import (
    ADAPTER_ID, RUNTIME, AuthorityUnavailableError, DisposableCodingWorkerAdapter,
    DisposableHarnessConfig,
)
from orchestra.contracts import (
    ExecutionGrant, FileScope, GrantValidationError, IsolationRequirements,
    NetworkPolicy, ResourceLimits, TaskSpec,
)


class MockAuthority:
    def __init__(self, error=None): self.calls, self.error = [], error
    def validate_grant(self, task, grant, *, adapter_id, runtime):
        self.calls.append((task.digest, grant.grant_id, adapter_id, runtime))
        if self.error: raise self.error


def task(*, instructions="Create a bounded disposable result.", seconds=5):
    return TaskSpec(task_id="task-adapter", request_id="request-adapter",
        repository="owner/repository", base_commit="a"*40,
        agent_id="disposable-coding-agent", model_provider="fake-provider",
        model_id="fake-model", coding_harness="disposable-harness",
        permitted_runtimes=(RUNTIME,), authorized_operations=("read_repository","edit_workspace","run_tests"),
        file_scope=FileScope(read_paths=(".",), write_paths=("src",)),
        network=NetworkPolicy("denied"), isolation=IsolationRequirements(
            filesystem="ephemeral_workspace", credentials="broker_only",
            git_metadata="read_only", sandbox="ephemeral_runner"),
        resource_limits=ResourceLimits(max_runtime_seconds=seconds, max_output_bytes=4096),
        approval_class="routine", consequence_class="routine_development",
        instructions=instructions)


def grant(value, *, adapter=ADAPTER_ID, runtime=RUNTIME, expires=120):
    now=datetime.now(timezone.utc)
    return ExecutionGrant(grant_id="grant-adapter", proposal_digest=value.digest,
        attempt_id="attempt-adapter", task_id=value.task_id, request_id=value.request_id,
        adapter_id=adapter, runtime=runtime, repository=value.repository,
        base_commit=value.base_commit, agent_id=value.agent_id,
        model_provider=value.model_provider, model_id=value.model_id,
        coding_harness=value.coding_harness, file_scope=value.file_scope,
        authorized_operations=value.authorized_operations, nonce="n"*48,
        issued_at=now.isoformat(), expires_at=(now+timedelta(seconds=expires)).isoformat(),
        issuer="authority:test")


class CodingWorkerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="orchestra-adapter-test-")
        self.root=Path(self.temp.name); self.authority=MockAuthority()
        self.adapter=DisposableCodingWorkerAdapter(self.root,self.authority)
    def tearDown(self): self.temp.cleanup()
    def wait(self, adapter, receipt, terminal=True):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=adapter.status(receipt)
            if not terminal or state.state in {"succeeded","failed","cancelled","unknown"}: return state
            time.sleep(.02)
        self.fail("adapter did not reach expected state")

    def test_valid_adapter_lifecycle_and_unverified_success(self):
        value=task(); issued=grant(value)
        self.assertIsInstance(self.adapter,AgentAdapter)
        self.assertTrue(self.adapter.preflight(value,issued).ok)
        receipt=self.adapter.dispatch(value,issued); self.assertEqual(self.wait(self.adapter,receipt).state,"succeeded")
        report=self.adapter.collect(receipt)
        self.assertEqual(report.outcome,"succeeded"); self.assertFalse(report.completion_verified)
        self.assertFalse((self.root/issued.attempt_id/"workspace").exists())

    def test_missing_or_invalid_authority_fails_closed(self):
        value=task(); issued=grant(value)
        adapter=DisposableCodingWorkerAdapter(self.root/"missing",None)
        self.assertFalse(adapter.preflight(value,issued).ok)
        with self.assertRaises(AuthorityUnavailableError): adapter.dispatch(value,issued)
        refused=DisposableCodingWorkerAdapter(self.root/"refused",MockAuthority(ValueError("rejected")))
        self.assertFalse(refused.preflight(value,issued).ok)

    def test_adapter_and_runtime_mismatch(self):
        value=task()
        with self.assertRaises(GrantValidationError): self.adapter.dispatch(value,grant(value,adapter="other-adapter"))
        with self.assertRaises(GrantValidationError): self.adapter.dispatch(value,grant(value,runtime="github_actions"))

    def test_modified_task_is_rejected(self):
        value=task(); issued=grant(value); changed=task(instructions="changed")
        with self.assertRaises(GrantValidationError): self.adapter.dispatch(changed,issued)

    def test_expired_grant_is_rejected(self):
        value=task(); now=datetime.now(timezone.utc)
        issued=replace(grant(value),issued_at=(now-timedelta(seconds=2)).isoformat(),expires_at=(now-timedelta(seconds=1)).isoformat())
        with self.assertRaises(GrantValidationError): self.adapter.dispatch(value,issued)

    def test_duplicate_dispatch_is_idempotent(self):
        value=task(); issued=grant(value)
        first=self.adapter.dispatch(value,issued); second=self.adapter.dispatch(value,issued)
        self.assertEqual(first,second); self.assertEqual(len(self.adapter._processes),1)
        self.wait(self.adapter,first); self.adapter.collect(first)

    def test_cancellation_targets_worker_process_group(self):
        adapter=DisposableCodingWorkerAdapter(self.root,self.authority,
            harness=DisposableHarnessConfig("sleep",10)); value=task(); receipt=adapter.dispatch(value,grant(value))
        result=adapter.cancel(receipt,"test cancellation")
        self.assertTrue(result.requested); self.assertEqual(result.provider_state,"cancelled")
        self.assertEqual(adapter.collect(receipt).outcome,"cancelled")

    def test_timeout_is_reported_without_retry(self):
        adapter=DisposableCodingWorkerAdapter(self.root,self.authority,
            harness=DisposableHarnessConfig("sleep",10)); value=task(seconds=1); receipt=adapter.dispatch(value,grant(value))
        self.assertEqual(self.wait(adapter,receipt).provider_state,"timed_out")
        self.assertEqual(adapter.collect(receipt).outcome,"timed_out")

    def test_interrupted_execution_is_failed(self):
        adapter=DisposableCodingWorkerAdapter(self.root,self.authority,
            harness=DisposableHarnessConfig("interrupt")); value=task(); receipt=adapter.dispatch(value,grant(value))
        state=self.wait(adapter,receipt); self.assertEqual(state.provider_state,"interrupted")
        self.assertEqual(adapter.collect(receipt).outcome,"interrupted")

    def test_unknown_outcome_recovery_is_ambiguous_and_never_retries(self):
        adapter=DisposableCodingWorkerAdapter(self.root,self.authority,
            harness=DisposableHarnessConfig("sleep",10)); value=task(); issued=grant(value); receipt=adapter.dispatch(value,issued)
        record=json.loads((self.root/issued.attempt_id/"job.json").read_text())
        os.killpg(record["pid"],signal.SIGKILL); adapter._processes[issued.attempt_id].wait(timeout=2)
        reopened=DisposableCodingWorkerAdapter(self.root,self.authority)
        recovered=reopened.recover(value,issued.attempt_id)
        self.assertEqual(recovered.state,"ambiguous"); self.assertEqual(reopened.status(receipt).state,"unknown")

    def test_recovery_adopts_a_live_process(self):
        adapter=DisposableCodingWorkerAdapter(self.root,self.authority,
            harness=DisposableHarnessConfig("sleep",1)); value=task(); issued=grant(value); receipt=adapter.dispatch(value,issued)
        reopened=DisposableCodingWorkerAdapter(self.root,self.authority)
        self.assertEqual(reopened.recover(value,issued.attempt_id).state,"recovered")
        adapter.cancel(receipt,"cleanup")

    def test_artifact_hash_is_recomputed_by_adapter(self):
        value=task(); receipt=self.adapter.dispatch(value,grant(value)); self.wait(self.adapter,receipt)
        result_path=self.root/receipt.attempt_id/"result.json"; result=json.loads(result_path.read_text()); result["artifact_sha256"]="0"*64; result_path.write_text(json.dumps(result))
        report=self.adapter.collect(receipt)
        self.assertEqual(report.outcome,"failed"); self.assertEqual(report.artifacts,())

    def test_artifact_symlink_is_rejected_without_reading_target(self):
        value=task(); receipt=self.adapter.dispatch(value,grant(value)); self.wait(self.adapter,receipt)
        artifact=self.root/receipt.attempt_id/"workspace/src/result.txt"
        target=self.root/"unrelated-secret"; target.write_text("must not be collected")
        artifact.unlink(); artifact.symlink_to(target)
        report=self.adapter.collect(receipt)
        self.assertEqual(report.outcome,"failed"); self.assertEqual(report.artifacts,())

    def test_no_credentials_or_production_side_effects(self):
        value=task(instructions="No secret is needed."); issued=grant(value); receipt=self.adapter.dispatch(value,issued); self.wait(self.adapter,receipt)
        content="\n".join(path.read_text(errors="replace") for path in (self.root/issued.attempt_id).glob("*.json"))
        for secret_name in ("API_KEY","TOKEN","PASSWORD","credential_file","/etc/orchestra","/home/hamed"):
            self.assertNotIn(secret_name,content)
        self.assertEqual(set(self.root.iterdir()),{self.root/issued.attempt_id})
        self.adapter.collect(receipt)

    def test_failure_produces_structured_untrusted_report(self):
        adapter=DisposableCodingWorkerAdapter(self.root,self.authority,
            harness=DisposableHarnessConfig("fail")); value=task(); receipt=adapter.dispatch(value,grant(value)); self.wait(adapter,receipt)
        report=adapter.collect(receipt); self.assertEqual(report.outcome,"failed"); self.assertFalse(report.completion_verified)

    def test_manifest_separates_provider_harness_runtime_and_adapter(self):
        manifest=self.adapter.describe()
        self.assertEqual(manifest.model_provider,"fake-provider")
        self.assertEqual(manifest.coding_harness,"disposable-harness")
        self.assertEqual(manifest.supported_runtimes,(RUNTIME,))
        self.assertNotEqual(ADAPTER_ID,manifest.agent_id)
        self.assertEqual(manifest.credential_refs,())
