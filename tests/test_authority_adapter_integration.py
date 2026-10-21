import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from orchestra.authority import (
    ExecutionAudience, execution_grant_from_dict,
)
from orchestra.authority_client import AuthorityClient, AuthorityClientError
from orchestra.authority_service import PeerIdentity, ServiceConfig
from orchestra.coding_worker_adapter import (
    ADAPTER_ID, RUNTIME, AdapterExecutionError, DisposableCodingWorkerAdapter,
    DisposableHarnessConfig,
)
from orchestra.contracts import (
    EvidenceReference, FileScope, IsolationRequirements, NetworkPolicy,
    ResourceLimits, TaskSpec, ValidationOutcome, VerificationRecord,
)
from tests.test_authority_service import CLIENT_UIDS, MutableClock, RunningService


def integration_task(suffix="one", *, seconds=5):
    return TaskSpec(task_id=f"task-{suffix}", request_id=f"request-{suffix}",
        repository="owner/integration", base_commit="a"*40,
        agent_id="disposable-coding-agent", model_provider="fake-provider",
        model_id="fake-model", coding_harness="disposable-harness",
        permitted_runtimes=(RUNTIME,), authorized_operations=(
            "read_repository", "edit_workspace", "run_tests"),
        file_scope=FileScope(read_paths=(".",), write_paths=("src",)),
        network=NetworkPolicy("denied"), isolation=IsolationRequirements(
            filesystem="ephemeral_workspace", credentials="broker_only",
            git_metadata="read_only", sandbox="ephemeral_runner"),
        resource_limits=ResourceLimits(max_runtime_seconds=seconds, max_output_bytes=4096),
        approval_class="routine", consequence_class="routine_development",
        instructions="Run the authenticated disposable integration.")


def integration_config(root, task):
    state=root/"s"; runtime=root/"r"
    state.mkdir(mode=0o700); runtime.mkdir(mode=0o750)
    return ServiceConfig(authority_id="authority:phase3c-test",
        service_uid=__import__("os").geteuid(), database_path=str(state/"a.db"),
        socket_path=str(runtime/"a.sock"), socket_gid=__import__("os").getgid(),
        peers=tuple(PeerIdentity(uid,role,role) for role,uid in CLIENT_UIDS.items()),
        audiences=(ExecutionAudience(adapter_id=ADAPTER_ID,runtime=RUNTIME,
            agent_id=task.agent_id,model_provider=task.model_provider,
            model_id=task.model_id,coding_harness=task.coding_harness),),
        grant_ttl_seconds=120,approval_ttl_seconds=300,stale_attempt_seconds=60,
        max_message_bytes=1_048_576,socket_timeout_seconds=5)


class AuthorityAdapterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="oa-",dir="/tmp")
        self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.task=integration_task(); self.clock=MutableClock()
        config=integration_config(self.root,self.task)
        self.assertLess(len(config.socket_path.encode("utf-8")),80)
        self.running=RunningService(config,clock=self.clock)
        self.running.__enter__(); self.addCleanup(self.running.__exit__,None,None,None)
        self.coordinator=self.running.client("coordinator")
        self.worker=self.running.client("worker"); self.verifier=self.running.client("verifier")
        self.human=self.running.client("human"); self.operator=self.running.client("operator")

    def issue(self, task=None, suffix="one"):
        task=task or self.task
        self.coordinator.propose(task,request_id=f"propose-{suffix}")
        self.human.decide_approval(task.digest,decision="approved",request_id=f"approve-{suffix}")
        issued=self.coordinator.request_execution(task,adapter_id=ADAPTER_ID,runtime=RUNTIME,
            idempotency_key=f"execution-{suffix}",request_id=f"issue-{suffix}")
        return execution_grant_from_dict(issued["grant"])

    def wait(self,adapter,receipt):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status=adapter.status(receipt)
            if status.state in {"succeeded","failed","cancelled","unknown"}: return status
            time.sleep(.02)
        self.fail("disposable worker did not finish")

    def test_actual_socket_consume_dispatch_report_and_independent_verification(self):
        grant=self.issue(); adapter=DisposableCodingWorkerAdapter(
            self.root/"jobs",self.coordinator,clock=self.clock)
        receipt=adapter.dispatch(self.task,grant)
        durable=__import__("json").loads((self.root/"jobs"/grant.attempt_id/"job.json").read_text())
        self.assertEqual(durable["state"],"running")
        self.assertEqual(durable["consume_receipt"]["grant_id"],grant.grant_id)
        self.assertIsNotNone(durable["consume_receipt"]["consumed_at"])
        self.worker.record_dispatch(grant.attempt_id,provider_run_id=receipt.provider_run_id,
            adapter_id=ADAPTER_ID,runtime=RUNTIME,request_id="worker-dispatch")
        self.assertEqual(self.wait(adapter,receipt).state,"succeeded")
        report=adapter.collect(receipt); self.assertFalse(report.completion_verified)
        reported=self.worker.record_report(report,request_id="worker-report")
        self.assertEqual(reported["state"],"reported"); self.assertFalse(reported["verified"])
        artifact=report.artifacts[0]
        verification=VerificationRecord(verification_id="verification-integration",
            task_id=self.task.task_id,proposal_digest=self.task.digest,
            attempt_id=grant.attempt_id,verifier_id=f"uid:{CLIENT_UIDS['verifier']}",
            method="independent-artifact-binding",
            evidence_checked=(EvidenceReference("artifact",artifact.uri,artifact.sha256),),
            outcomes=(ValidationOutcome("artifact digest independently accepted","pass"),),
            status="pass",verified_at=self.clock.current.isoformat())
        decision=self.verifier.record_verification(verification,request_id="verify-integration")
        self.assertEqual(decision["final_state"],"verified")

    def test_concurrent_adapters_cannot_consume_or_launch_twice(self):
        grant=self.issue(); adapters=[DisposableCodingWorkerAdapter(
            self.root/f"jobs-{index}",self.coordinator,clock=self.clock,
            harness=DisposableHarnessConfig("sleep",1)) for index in range(2)]
        receipts=[]; errors=[]
        def dispatch(adapter):
            try: receipts.append((adapter,adapter.dispatch(self.task,grant)))
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=dispatch,args=(adapter,)) for adapter in adapters]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(receipts),1); self.assertEqual(len(errors),1)
        self.assertEqual(self.coordinator.grant(grant.grant_id)["consumed_at"] is not None,True)
        winner,receipt=receipts[0]; winner.cancel(receipt,"test cleanup")

    def test_concurrent_adapters_sharing_state_serialize_one_launch(self):
        grant=self.issue(); state=self.root/"shared-jobs"
        adapters=[DisposableCodingWorkerAdapter(
            state,self.coordinator,clock=self.clock,
            harness=DisposableHarnessConfig("sleep",1)) for _ in range(2)]
        receipts=[]; errors=[]
        def dispatch(adapter):
            try: receipts.append(adapter.dispatch(self.task,grant))
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=dispatch,args=(adapter,)) for adapter in adapters]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(receipts),2); self.assertEqual(receipts[0],receipts[1])
        owners=[adapter for adapter in adapters if grant.attempt_id in adapter._processes]
        self.assertEqual(len(owners),1)
        owners[0].cancel(receipts[0],"test cleanup")

    def test_shared_state_serializes_when_flock_is_process_scoped(self):
        grant=self.issue(); state=self.root/"shared-jobs"
        adapters=[DisposableCodingWorkerAdapter(
            state,self.coordinator,clock=self.clock,
            harness=DisposableHarnessConfig("sleep",1)) for _ in range(2)]
        receipts=[]; errors=[]
        def dispatch(adapter):
            try: receipts.append(adapter.dispatch(self.task,grant))
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=dispatch,args=(adapter,)) for adapter in adapters]
        with mock.patch("orchestra.coding_worker_adapter.fcntl.flock"):
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(receipts),2); self.assertEqual(receipts[0],receipts[1])
        owners=[adapter for adapter in adapters if grant.attempt_id in adapter._processes]
        self.assertEqual(len(owners),1)
        owners[0].cancel(receipts[0],"test cleanup")

    def test_duplicate_dispatch_reuses_one_consumption_and_one_process(self):
        grant=self.issue(); adapter=DisposableCodingWorkerAdapter(
            self.root/"jobs",self.coordinator,clock=self.clock,
            harness=DisposableHarnessConfig("sleep",1))
        first=adapter.dispatch(self.task,grant)
        second=adapter.dispatch(self.task,grant)
        self.assertEqual(first,second)
        record=__import__("json").loads(
            (self.root/"jobs"/grant.attempt_id/"job.json").read_text())
        self.assertEqual(record["pid"],adapter._processes[grant.attempt_id].pid)
        adapter.cancel(first,"cleanup")

    def test_exact_task_attempt_and_audience_binding_is_enforced(self):
        grant=self.issue()
        modified=replace(self.task,instructions="substituted after approval")
        adapter=DisposableCodingWorkerAdapter(self.root/"modified",self.coordinator,
            clock=self.clock)
        self.assertFalse(adapter.preflight(modified,grant).ok)
        forged=replace(grant,attempt_id="attempt-forged")
        forged_adapter=DisposableCodingWorkerAdapter(
            self.root/"forged",self.coordinator,clock=self.clock)
        self.assertFalse(forged_adapter.preflight(self.task,forged).ok)
        wrong_audience=replace(grant,adapter_id="another-adapter")
        audience_adapter=DisposableCodingWorkerAdapter(
            self.root/"audience",self.coordinator,clock=self.clock)
        self.assertFalse(audience_adapter.preflight(self.task,wrong_audience).ok)
        self.assertIsNone(self.coordinator.grant(grant.grant_id)["consumed_at"])

    def test_crash_before_consume_recovers_with_same_durable_intent(self):
        grant=self.issue();
        def fault(stage,_record):
            if stage=="after_intent": raise RuntimeError("simulated crash")
        crashed=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,
            clock=self.clock,fault_injector=fault)
        with self.assertRaises(RuntimeError): crashed.dispatch(self.task,grant)
        self.assertIsNone(self.coordinator.grant(grant.grant_id)["consumed_at"])
        recovered=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,clock=self.clock)
        result=recovered.recover(self.task,grant.attempt_id)
        self.assertEqual(result.state,"recovered")
        receipt=recovered._receipt(__import__("json").loads((self.root/"jobs"/grant.attempt_id/"job.json").read_text()))
        recovered.cancel(receipt,"cleanup")

    def test_crash_after_consume_before_launch_recovers_without_reconsume(self):
        grant=self.issue()
        def fault(stage,_record):
            if stage=="after_consume": raise RuntimeError("simulated crash")
        crashed=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,
            clock=self.clock,fault_injector=fault)
        with self.assertRaises(RuntimeError): crashed.dispatch(self.task,grant)
        consumed=self.coordinator.grant(grant.grant_id)["consumed_at"]; self.assertIsNotNone(consumed)
        disconnected=DisposableCodingWorkerAdapter(
            self.root/"jobs",None,clock=self.clock)
        self.assertEqual(
            disconnected.recover(self.task,grant.attempt_id).state,"failed")
        self.assertFalse((self.root/"jobs"/grant.attempt_id/"workspace").exists())
        recovered=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,clock=self.clock)
        self.assertEqual(recovered.recover(self.task,grant.attempt_id).state,"recovered")
        self.assertEqual(self.coordinator.grant(grant.grant_id)["consumed_at"],consumed)
        record=__import__("json").loads((self.root/"jobs"/grant.attempt_id/"job.json").read_text())
        recovered.cancel(recovered._receipt(record),"cleanup")

    def test_crash_during_safe_launch_preparation_recovers_without_reconsume(self):
        grant=self.issue()
        def fault(stage,_record):
            if stage=="after_spec": raise RuntimeError("simulated crash")
        crashed=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,
            clock=self.clock,fault_injector=fault)
        with self.assertRaises(RuntimeError): crashed.dispatch(self.task,grant)
        consumed=self.coordinator.grant(grant.grant_id)["consumed_at"]
        recovered=DisposableCodingWorkerAdapter(
            self.root/"jobs",self.coordinator,clock=self.clock)
        self.assertEqual(recovered.recover(self.task,grant.attempt_id).state,"recovered")
        self.assertEqual(self.coordinator.grant(grant.grant_id)["consumed_at"],consumed)
        record=__import__("json").loads(
            (self.root/"jobs"/grant.attempt_id/"job.json").read_text())
        recovered.cancel(recovered._receipt(record),"cleanup")

    def test_crash_at_launch_boundary_is_unknown_and_never_retried(self):
        grant=self.issue()
        def fault(stage,_record):
            if stage=="after_launch_intent": raise RuntimeError("simulated crash")
        crashed=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,
            clock=self.clock,fault_injector=fault)
        with self.assertRaises(RuntimeError): crashed.dispatch(self.task,grant)
        recovered=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,clock=self.clock)
        result=recovered.recover(self.task,grant.attempt_id)
        self.assertEqual(result.state,"ambiguous")
        with self.assertRaises(AdapterExecutionError): recovered.dispatch(self.task,grant)

    def test_crash_during_execution_adopts_exact_live_process(self):
        grant=self.issue()
        def fault(stage,_record):
            if stage=="after_process_start": raise RuntimeError("simulated crash")
        crashed=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,
            clock=self.clock,harness=DisposableHarnessConfig("sleep",1),
            fault_injector=fault)
        with self.assertRaises(RuntimeError): crashed.dispatch(self.task,grant)
        reopened=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,
            clock=self.clock)
        self.assertEqual(reopened.recover(self.task,grant.attempt_id).state,"recovered")
        record=__import__("json").loads(
            (self.root/"jobs"/grant.attempt_id/"job.json").read_text())
        reopened.cancel(reopened._receipt(record),"cleanup")
        crashed._reap(grant.attempt_id)

    def test_authority_unavailable_or_unauthenticated_fails_before_intent(self):
        grant=self.issue()
        unavailable=DisposableCodingWorkerAdapter(self.root/"missing",
            AuthorityClient(self.root/"absent.sock",timeout=.1),clock=self.clock)
        self.assertFalse(unavailable.preflight(self.task,grant).ok)
        with self.assertRaises(AdapterExecutionError): unavailable.dispatch(self.task,grant)
        rogue=DisposableCodingWorkerAdapter(self.root/"rogue",
            self.running.client_as_uid(99_999),clock=self.clock)
        self.assertFalse(rogue.preflight(self.task,grant).ok)
        with self.assertRaises(AdapterExecutionError): rogue.dispatch(self.task,grant)
        self.assertFalse((self.root/"rogue"/grant.attempt_id).exists())

    def test_cancellation_after_consume_remains_unverifiable(self):
        task=integration_task("cancel"); grant=self.issue(task,"cancel")
        adapter=DisposableCodingWorkerAdapter(self.root/"cancel-jobs",self.coordinator,
            clock=self.clock,harness=DisposableHarnessConfig("sleep",10))
        receipt=adapter.dispatch(task,grant)
        self.worker.record_dispatch(grant.attempt_id,provider_run_id=receipt.provider_run_id,
            adapter_id=ADAPTER_ID,runtime=RUNTIME,request_id="worker-dispatch-cancel")
        cancellation=self.coordinator.cancel_attempt(grant.attempt_id,reason="operator request",
            request_id="authority-cancel")
        self.assertEqual(cancellation["final_state"],"cancelled_uncertain")
        adapter.cancel(receipt,"authority cancellation")
        report=adapter.collect(receipt)
        self.worker.record_report(report,request_id="late-report")
        self.assertFalse(self.coordinator.attempt(grant.attempt_id)["verified"])

    def test_completion_survives_adapter_restart_before_report_persistence(self):
        grant=self.issue(); adapter=DisposableCodingWorkerAdapter(
            self.root/"jobs",self.coordinator,clock=self.clock)
        receipt=adapter.dispatch(self.task,grant); self.wait(adapter,receipt)
        reopened=DisposableCodingWorkerAdapter(self.root/"jobs",self.coordinator,clock=self.clock)
        self.assertEqual(reopened.recover(self.task,grant.attempt_id).state,"recovered")
        report=reopened.collect(receipt)
        self.assertEqual(report.outcome,"succeeded"); self.assertFalse(report.completion_verified)
