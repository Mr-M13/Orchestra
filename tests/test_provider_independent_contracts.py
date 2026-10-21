import json
import re
import unittest
from dataclasses import replace
from pathlib import Path

from omcp.controller import Worker

from orchestra.adapters import (
    AgentAdapter,
    PreflightResult,
    UnavailableCombination,
    default_shadow_registry,
)
from orchestra.contracts import (
    AgentManifest,
    ArtifactReference,
    CapabilityClaim,
    Concurrency,
    ContractValidationError,
    CostCharacteristics,
    CredentialReference,
    EvidenceReference,
    ExecutionError,
    ExecutionGrant,
    ExecutionReport,
    FileScope,
    GrantValidationError,
    IsolationRequirements,
    NetworkPolicy,
    RegistryConflictError,
    ResourceLimits,
    ResourceUsage,
    TaskSpec,
    TestEvidence,
    ToolRequirement,
    ValidationOutcome,
    VerificationRecord,
    canonical_json,
)


BASE_COMMIT = "a" * 40
NOW = "2026-10-21T00:00:00+00:00"
EXPIRES = "2026-10-21T01:00:00+00:00"
NONCE = "n" * 64
EVIDENCE = "artifact:evidence"


def make_task(**overrides):
    values = {
        "task_id": "task-1",
        "request_id": "request-1",
        "repository": "owner/repo",
        "base_commit": BASE_COMMIT,
        "agent_id": "codex-github-actions",
        "model_provider": "openai",
        "model_id": "gpt-5-codex",
        "coding_harness": "codex-cli",
        "permitted_runtimes": ("github_actions",),
        "authorized_operations": (
            "read_repository", "edit_workspace", "run_commands", "run_tests",
        ),
        "file_scope": FileScope(
            read_paths=(".",), write_paths=("src", "tests"),
        ),
        "network": NetworkPolicy("denied"),
        "isolation": IsolationRequirements(
            filesystem="ephemeral_workspace",
            credentials="runner_scoped",
            git_metadata="read_only",
            sandbox="ephemeral_runner",
        ),
        "resource_limits": ResourceLimits(
            max_runtime_seconds=600, max_output_bytes=65_536,
        ),
        "approval_class": "routine",
        "consequence_class": "routine_development",
        "instructions": "Implement the requested change.",
    }
    values.update(overrides)
    return TaskSpec(**values)


def make_manifest(**overrides):
    values = {
        "agent_id": "test-agent",
        "display_name": "Test Agent",
        "agent_version": "1.0.0",
        "agent_class": "coding",
        "model_provider": "openai",
        "model_ids": ("gpt-5-codex",),
        "coding_harness": "codex-cli",
        "supported_runtimes": ("local_process",),
        "capabilities": (
            CapabilityClaim("read_repository"),
            CapabilityClaim("edit_workspace"),
        ),
        "tool_requirements": (ToolRequirement("git"),),
        "isolation": IsolationRequirements(),
        "network": NetworkPolicy("denied"),
        "credential_refs": (CredentialReference("env:TEST_AGENT_TOKEN"),),
        "resource_limits": ResourceLimits(),
        "concurrency": Concurrency(),
        "cost": CostCharacteristics(),
    }
    values.update(overrides)
    return AgentManifest(**values)


def make_grant(task, **overrides):
    values = {
        "grant_id": "grant-1",
        "proposal_digest": task.digest,
        "attempt_id": "attempt-1",
        "task_id": task.task_id,
        "request_id": task.request_id,
        "adapter_id": "codex-github-actions",
        "runtime": "github_actions",
        "repository": task.repository,
        "base_commit": task.base_commit,
        "agent_id": task.agent_id,
        "model_provider": task.model_provider,
        "model_id": task.model_id,
        "coding_harness": task.coding_harness,
        "file_scope": task.file_scope,
        "authorized_operations": task.authorized_operations,
        "nonce": NONCE,
        "issued_at": NOW,
        "expires_at": EXPIRES,
        "issuer": "authority:test",
    }
    values.update(overrides)
    return ExecutionGrant(**values)


def make_report(task, **overrides):
    values = {
        "report_id": "report-1",
        "task_id": task.task_id,
        "proposal_digest": task.digest,
        "attempt_id": "attempt-1",
        "provider_run_id": "github:owner/repo:123",
        "adapter_id": "codex-github-actions",
        "runtime": "github_actions",
        "agent_id": task.agent_id,
        "model_provider": task.model_provider,
        "model_id": task.model_id,
        "coding_harness": task.coding_harness,
        "outcome": "succeeded",
        "artifacts": (
            ArtifactReference("artifact:result", "b" * 64, "text/plain", 42),
        ),
        "commit_sha": BASE_COMMIT,
        "pull_request": "owner/repo#42",
        "test_evidence": (
            TestEvidence("unit tests", "passed", "artifact:tests"),
        ),
        "errors": (),
        "resource_usage": ResourceUsage(
            wall_time_seconds=12.5, cpu_seconds=7.0, cost_usd=0.01,
        ),
        "started_at": NOW,
        "finished_at": "2026-10-21T00:01:00+00:00",
    }
    values.update(overrides)
    return ExecutionReport(**values)


def make_verification(task, **overrides):
    values = {
        "verification_id": "verification-1",
        "task_id": task.task_id,
        "proposal_digest": task.digest,
        "attempt_id": "attempt-1",
        "verifier_id": "local:independent-reviewer",
        "method": "independent-test-run",
        "evidence_checked": (
            EvidenceReference("test_log", "artifact:tests", "b" * 64),
        ),
        "outcomes": (ValidationOutcome("unit tests pass", "pass"),),
        "status": "pass",
        "verified_at": NOW,
    }
    values.update(overrides)
    return VerificationRecord(**values)


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_and_digest(self):
        manifest = make_manifest()
        self.assertEqual(manifest.agent_id, "test-agent")
        self.assertEqual(manifest.capability_names, ("edit_workspace", "read_repository"))
        self.assertEqual(len(manifest.manifest_digest), 64)
        self.assertEqual(manifest.to_dict()["manifest_digest"], manifest.manifest_digest)
        self.assertEqual(json.loads(manifest.canonical_json())["agent_id"], "test-agent")

    def test_invalid_capabilities_are_rejected(self):
        with self.assertRaisesRegex(ContractValidationError, "unknown capability"):
            CapabilityClaim("delete_everything")
        with self.assertRaisesRegex(ContractValidationError, "duplicate capability"):
            make_manifest(capabilities=(
                CapabilityClaim("edit_workspace"),
                CapabilityClaim("edit_workspace"),
            ))

    def test_response_only_agent_cannot_claim_coding_capabilities(self):
        with self.assertRaisesRegex(ContractValidationError, "response-only"):
            make_manifest(
                agent_class="response_only",
                coding_harness="none",
                capabilities=(CapabilityClaim("edit_workspace"),),
                tool_requirements=(),
                isolation=IsolationRequirements(filesystem="none", git_metadata="none"),
            )

    def test_coding_agent_must_claim_a_coding_capability(self):
        with self.assertRaisesRegex(ContractValidationError, "coding capability"):
            make_manifest(capabilities=(CapabilityClaim("respond"),))

    def test_coding_agent_cannot_use_provider_api_runtime(self):
        with self.assertRaisesRegex(ContractValidationError, "workspace-bearing"):
            make_manifest(supported_runtimes=("provider_api",))

    def test_unknown_runtime_and_invalid_credential_reference_are_rejected(self):
        with self.assertRaisesRegex(ContractValidationError, "unknown value"):
            make_manifest(supported_runtimes=("kubernetes",))
        with self.assertRaisesRegex(ContractValidationError, "credential reference"):
            CredentialReference("sk-live-secret-value")

    def test_verified_capability_requires_evidence(self):
        with self.assertRaisesRegex(ContractValidationError, "evidence reference"):
            CapabilityClaim("edit_workspace", "verified")

    def test_verified_manifest_requires_verified_capabilities(self):
        with self.assertRaisesRegex(ContractValidationError, "unverified capabilities"):
            make_manifest(
                verification_status="verified",
                verification_evidence_ref=EVIDENCE,
                capabilities=(CapabilityClaim("edit_workspace", "unverified"),),
            )


class TaskSpecTests(unittest.TestCase):
    def test_canonical_digest_regression_vector(self):
        self.assertEqual(
            make_task().digest,
            "bec10ecb2d9be2c77674a1a3bb472374eb7b40a8adbbf66101b75c6375e54a33",
        )

    def test_canonical_hash_is_stable_across_equivalent_serialisation(self):
        first = make_task(
            authorized_operations=(
                "read_repository", "edit_workspace", "run_commands", "run_tests",
            ),
            file_scope=FileScope(
                read_paths=(".", "docs"), write_paths=("src", "tests"),
            ),
            permitted_runtimes=("github_actions", "local_process"),
        )
        second = make_task(
            authorized_operations=(
                "run_tests", "run_commands", "edit_workspace", "read_repository",
            ),
            file_scope=FileScope(
                read_paths=("docs", "."), write_paths=("tests", "src"),
            ),
            permitted_runtimes=("local_process", "github_actions"),
        )
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(
            canonical_json(json.loads(first.canonical_json())),
            first.canonical_json(),
        )

    def test_digest_changes_after_each_security_relevant_field_change(self):
        base = make_task()
        variants = {
            "task_id": {"task_id": "task-2"},
            "request_id": {"request_id": "request-2"},
            "repository": {"repository": "owner/other"},
            "base_commit": {"base_commit": "b" * 40},
            "agent_id": {"agent_id": "codex-github-actions-2"},
            "model_provider": {"model_provider": "azure"},
            "model_id": {"model_id": "gpt-5-codex-mini"},
            "coding_harness": {"coding_harness": "codex-cli-v2"},
            "permitted_runtimes": {"permitted_runtimes": ("local_process",)},
            "authorized_operations": {
                "authorized_operations": base.authorized_operations + ("create_branch",),
            },
            "file_scope": {
                "file_scope": FileScope(
                    read_paths=(".", "docs"), write_paths=("src", "tests"),
                ),
            },
            "network": {
                "network": NetworkPolicy("allowlisted", ("example.com",)),
                "authorized_operations": base.authorized_operations + ("access_network",),
            },
            "isolation": {
                "isolation": IsolationRequirements(
                    filesystem="workspace_only",
                    credentials="broker_only",
                    git_metadata="read_only",
                    sandbox="required",
                ),
            },
            "resource_limits": {
                "resource_limits": ResourceLimits(
                    max_runtime_seconds=900, max_output_bytes=65_536,
                ),
            },
            "approval_class": {"approval_class": "sensitive"},
            "consequence_class": {
                "consequence_class": "security_change",
                "approval_class": "sensitive",
            },
            "instructions": {"instructions": "Different instructions."},
        }
        for name, overrides in variants.items():
            with self.subTest(field=name):
                changed = make_task(**overrides)
                self.assertNotEqual(base.digest, changed.digest)
        referenced = make_task(
            instructions=None, instruction_ref="sha256:" + "d" * 64,
        )
        other_reference = make_task(
            instructions=None, instruction_ref="sha256:" + "e" * 64,
        )
        self.assertNotEqual(base.digest, referenced.digest)
        self.assertNotEqual(referenced.digest, other_reference.digest)

    def test_task_requires_exactly_one_instruction_source(self):
        with self.assertRaisesRegex(ContractValidationError, "exactly one"):
            make_task(instructions="inline", instruction_ref="sha256:" + "d" * 64)
        with self.assertRaisesRegex(ContractValidationError, "exactly one"):
            make_task(instructions=None)
        with self.assertRaisesRegex(ContractValidationError, "blank"):
            make_task(instructions="   ")

    def test_task_rejects_invalid_security_scope(self):
        with self.assertRaisesRegex(ContractValidationError, "write path"):
            make_task(
                authorized_operations=("read_repository", "run_tests"),
                file_scope=FileScope(read_paths=(".",), write_paths=("src",)),
            )
        with self.assertRaisesRegex(ContractValidationError, "relative paths"):
            make_task(file_scope=FileScope(write_paths=("/tmp",)))
        with self.assertRaisesRegex(ContractValidationError, "unsafe path"):
            make_task(file_scope=FileScope(write_paths=("src/../etc",)))
        with self.assertRaisesRegex(ContractValidationError, "workspace root"):
            make_task(file_scope=FileScope(write_paths=(".",)))
        with self.assertRaisesRegex(ContractValidationError, "denied network"):
            make_task(
                authorized_operations=(
                    "read_repository", "edit_workspace", "access_network",
                ),
            )
        with self.assertRaisesRegex(ContractValidationError, "sensitive approval"):
            make_task(
                consequence_class="security_change", approval_class="routine",
            )


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.task = make_task()
        self.grant = make_grant(self.task)

    def test_valid_grant_binding(self):
        self.grant.validate_binding(
            self.task,
            attempt_id="attempt-1",
            adapter_id="codex-github-actions",
            runtime="github_actions",
            now=NOW,
        )
        self.assertFalse(self.grant.is_expired(NOW))
        self.assertEqual(
            self.grant.canonical_json(),
            make_grant(self.task).canonical_json(),
        )

    def test_expired_and_not_yet_valid_grants_are_rejected(self):
        with self.assertRaisesRegex(GrantValidationError, "grant_expired"):
            self.grant.validate_binding(
                self.task,
                attempt_id="attempt-1",
                adapter_id="codex-github-actions",
                runtime="github_actions",
                now=EXPIRES,
            )
        with self.assertRaisesRegex(GrantValidationError, "grant_not_yet_valid"):
            self.grant.validate_binding(
                self.task,
                attempt_id="attempt-1",
                adapter_id="codex-github-actions",
                runtime="github_actions",
                now="2026-10-20T23:59:00+00:00",
            )

    def test_consumed_grant_is_rejected(self):
        with self.assertRaisesRegex(GrantValidationError, "already_consumed"):
            self.grant.validate_binding(
                self.task,
                attempt_id="attempt-1",
                adapter_id="codex-github-actions",
                runtime="github_actions",
                now=NOW,
                consumed=True,
            )

    def test_grant_must_be_single_use_and_unsigned(self):
        with self.assertRaisesRegex(ContractValidationError, "single-use"):
            make_grant(self.task, single_use=False)
        with self.assertRaisesRegex(ContractValidationError, "unsigned"):
            make_grant(self.task, integrity="signed-production-key")

    def test_grant_rejects_short_nonce_and_invalid_expiry(self):
        with self.assertRaisesRegex(ContractValidationError, "nonce"):
            make_grant(self.task, nonce="short")
        with self.assertRaisesRegex(ContractValidationError, "after issued_at"):
            make_grant(self.task, expires_at=NOW)

    def test_attempt_audience_and_runtime_mismatches_are_rejected(self):
        with self.assertRaisesRegex(GrantValidationError, "attempt_id_mismatch"):
            self.grant.validate_binding(
                self.task,
                attempt_id="attempt-2",
                adapter_id="codex-github-actions",
                runtime="github_actions",
                now=NOW,
            )
        with self.assertRaisesRegex(GrantValidationError, "adapter_audience_mismatch"):
            self.grant.validate_binding(
                self.task,
                attempt_id="attempt-1",
                adapter_id="other-adapter",
                runtime="github_actions",
                now=NOW,
            )
        with self.assertRaisesRegex(GrantValidationError, "runtime_audience_mismatch"):
            self.grant.validate_binding(
                self.task,
                attempt_id="attempt-1",
                adapter_id="codex-github-actions",
                runtime="local_process",
                now=NOW,
            )
        restricted_task = make_task(permitted_runtimes=("local_process",))
        with self.assertRaisesRegex(GrantValidationError, "runtime_not_permitted"):
            make_grant(restricted_task).validate_binding(
                restricted_task,
                attempt_id="attempt-1",
                adapter_id="codex-github-actions",
                runtime="github_actions",
                now=NOW,
            )

    def test_proposal_identity_and_scope_mismatches_are_rejected(self):
        other_task = make_task(instructions="Different work.")
        with self.assertRaisesRegex(GrantValidationError, "proposal_digest_mismatch"):
            self.grant.validate_binding(
                other_task,
                attempt_id="attempt-1",
                adapter_id="codex-github-actions",
                runtime="github_actions",
                now=NOW,
            )
        mismatches = {
            "task_identity_mismatch": {"task_id": "task-2"},
            "repository_or_base_mismatch": {"repository": "owner/other"},
            "agent_identity_mismatch": {"model_id": "other-model"},
            "file_scope_mismatch": {
                "file_scope": FileScope(read_paths=(".",), write_paths=("src",)),
            },
            "authorized_operations_mismatch": {
                "authorized_operations": self.task.authorized_operations + (
                    "create_branch",
                ),
            },
        }
        for reason, overrides in mismatches.items():
            with self.subTest(reason=reason):
                grant = make_grant(self.task, **overrides)
                with self.assertRaisesRegex(GrantValidationError, reason):
                    grant.validate_binding(
                        self.task,
                        attempt_id="attempt-1",
                        adapter_id="codex-github-actions",
                        runtime="github_actions",
                        now=NOW,
                    )


class ReportAndVerificationTests(unittest.TestCase):
    def setUp(self):
        self.task = make_task()
        self.report = make_report(self.task)

    def test_worker_success_is_untrusted_and_never_self_verifying(self):
        self.assertFalse(self.report.completion_verified)
        self.assertEqual(self.report.trust, "untrusted_worker_report")
        self.assertNotIn("verified", self.report.to_dict())
        self.assertFalse(self.report.is_verified_by(make_verification(
            self.task, status="fail", outcomes=(ValidationOutcome("tests", "fail"),),
            failure_reasons=("tests failed",),
        )))

    def test_separate_passing_verification_can_verify_the_report(self):
        verification = make_verification(self.task)
        self.assertTrue(verification.verified_completion)
        self.assertTrue(self.report.is_verified_by(verification))
        self.assertFalse(self.report.is_verified_by(make_verification(
            self.task, attempt_id="attempt-2",
        )))

    def test_report_schema_rejects_invalid_values(self):
        invalid_overrides = {
            "unknown outcome": {"outcome": "probably-fine"},
            "invalid commit": {"commit_sha": "abc"},
            "invalid pull request": {"pull_request": "not-a-pr"},
            "invalid test evidence type": {"test_evidence": (object(),)},
            "invalid error type": {"errors": (object(),)},
            "trust escalation": {"trust": "trusted"},
            "time reversal": {"finished_at": "2026-10-20T23:59:00+00:00"},
        }
        for name, overrides in invalid_overrides.items():
            with self.subTest(case=name):
                with self.assertRaises(ContractValidationError):
                    make_report(self.task, **overrides)
        with self.assertRaisesRegex(ContractValidationError, "SHA-256"):
            ArtifactReference("artifact:result", "not-a-digest")
        with self.assertRaisesRegex(ContractValidationError, "test evidence status"):
            TestEvidence("unit", "green")
        with self.assertRaisesRegex(ContractValidationError, "error code"):
            ExecutionError("Not A Code", "failed")

    def test_verification_schema_rejects_false_pass(self):
        with self.assertRaisesRegex(ContractValidationError, "every outcome"):
            make_verification(self.task, outcomes=(ValidationOutcome("tests", "fail"),))
        with self.assertRaisesRegex(ContractValidationError, "failure reasons"):
            make_verification(self.task, failure_reasons=("still broken",))
        with self.assertRaisesRegex(ContractValidationError, "checked evidence"):
            make_verification(self.task, evidence_checked=())
        with self.assertRaisesRegex(ContractValidationError, "at least one outcome"):
            make_verification(self.task, outcomes=())
        with self.assertRaisesRegex(ContractValidationError, "unknown verification"):
            make_verification(self.task, status="green")

    def test_verification_is_bound_to_proposal_and_attempt(self):
        verification = make_verification(self.task)
        other = make_task(instructions="Other work.")
        self.assertFalse(make_verification(
            other, proposal_digest=other.digest,
        ).verifies(self.report))
        self.assertFalse(make_verification(
            self.task, attempt_id="attempt-2",
        ).verifies(self.report))


class ShadowRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = default_shadow_registry()

    def test_default_registry_represents_current_agents_without_operating_them(self):
        self.assertEqual(
            [manifest.agent_id for manifest in self.registry.agents],
            [
                "codex-github-actions",
                "deepseek-direct-response",
                "deepseek-opencode-github-actions",
                "hamed-ina-codex-local",
            ],
        )
        self.assertEqual(self.registry.operational_agents, ())
        self.assertEqual(
            self.registry.catalogue()["agent_classes"]["coding"],
            [
                "codex-github-actions",
                "deepseek-opencode-github-actions",
                "hamed-ina-codex-local",
            ],
        )
        self.assertEqual(
            self.registry.catalogue()["agent_classes"]["response_only"],
            ["deepseek-direct-response"],
        )
        self.assertTrue(self.registry.catalogue()["shadow_mode"])
        self.assertFalse(self.registry.catalogue()["takes_over_execution"])

    def test_configured_agents_reflect_the_current_local_runtime(self):
        self.assertIn("hamed-ina-codex-local", self.registry.configured_agents)
        self.assertIn("deepseek-direct-response", self.registry.configured_agents)
        self.assertNotIn("codex-github-actions", self.registry.configured_agents)
        self.assertNotIn(
            "deepseek-opencode-github-actions", self.registry.configured_agents,
        )
        self.assertFalse(
            self.registry.registration("codex-github-actions").operational,
        )
        self.assertFalse(
            self.registry.registration("deepseek-opencode-github-actions").configured,
        )

    def test_capability_discovery_marks_existing_claims_unverified(self):
        for agent_id in self.registry.configured_agents:
            self.assertEqual(self.registry.verified_capabilities(agent_id), ())
            self.assertTrue(self.registry.unverified_capabilities(agent_id))
        self.assertIn(
            "edit_workspace",
            [claim.capability for claim in self.registry.capabilities(
                "codex-github-actions"
            )],
        )
        self.assertEqual(
            self.registry.runtimes("hamed-ina-codex-local"), ("local_process",),
        )

    def test_response_only_deepseek_is_not_advertised_as_a_coding_worker(self):
        manifest = self.registry.get("deepseek-direct-response")
        self.assertEqual(manifest.agent_class, "response_only")
        self.assertEqual(manifest.capability_names, ("respond",))
        self.assertNotIn("edit_workspace", manifest.capability_names)
        self.assertEqual(manifest.coding_harness, "none")
        self.assertFalse(self.registry.availability(
            "deepseek-direct-response", capability="edit_workspace",
        ).available)
        self.assertFalse(self.registry.availability(
            "deepseek-direct-response", runtime="github_actions",
        ).available)
        self.assertTrue(self.registry.availability(
            "deepseek-direct-response", runtime="provider_api",
        ).available)

    def test_unsupported_model_harness_and_runtime_combinations(self):
        availability = self.registry.availability(
            "codex-github-actions",
            runtime="local_process",
            model_id="other-model",
            coding_harness="opencode",
        )
        self.assertFalse(availability.available)
        self.assertIn("runtime_not_supported", availability.reasons)
        self.assertIn("model_not_supported", availability.reasons)
        self.assertIn("harness_not_supported", availability.reasons)
        with self.assertRaisesRegex(ContractValidationError, "unknown runtime"):
            self.registry.availability("codex-github-actions", runtime="kubernetes")

    def test_unavailable_combinations_are_exposed(self):
        combinations = self.registry.unavailable_combinations
        self.assertTrue(combinations)
        reasons = {combination.reason_code for combination in combinations}
        self.assertIn("response_only_runtime_not_supported", reasons)
        self.assertIn("response_only_no_coding", reasons)
        self.assertIn("runtime_not_supported", reasons)
        self.assertFalse(self.registry.availability(
            "hamed-ina-codex-local", runtime="github_actions",
        ).available)

    def test_verified_capability_discovery_is_explicit(self):
        verified = make_manifest(
            agent_id="verified-agent",
            verification_status="verified",
            verification_evidence_ref=EVIDENCE,
            capabilities=(
                CapabilityClaim("read_repository", "verified", EVIDENCE),
                CapabilityClaim("edit_workspace", "verified", EVIDENCE),
            ),
        )
        self.registry.register_manifest(verified)
        self.assertEqual(
            self.registry.verified_capabilities("verified-agent"),
            ("edit_workspace", "read_repository"),
        )
        self.assertEqual(self.registry.unverified_capabilities("verified-agent"), ())

    def test_duplicate_ids_and_conflicting_definitions_are_rejected(self):
        manifest = make_manifest()
        self.registry.register_manifest(manifest)
        with self.assertRaisesRegex(RegistryConflictError, "duplicate agent id"):
            self.registry.register_manifest(manifest)
        with self.assertRaisesRegex(RegistryConflictError, "duplicate agent id"):
            self.registry.register_manifest(
                replace(manifest, display_name="Conflicting definition"),
            )
        self.assertEqual(
            self.registry.get("test-agent").display_name, "Test Agent",
        )

    def test_failed_registration_is_atomic(self):
        manifest = make_manifest(agent_id="atomic-agent")
        conflicting = UnavailableCombination(
            agent_id="another-agent",
            runtime="local_process",
            reason_code="runtime_not_supported",
            explanation="Wrong agent.",
        )
        with self.assertRaisesRegex(RegistryConflictError, "different agent"):
            self.registry.register_manifest(manifest, unavailable=(conflicting,))
        with self.assertRaises(KeyError):
            self.registry.get("atomic-agent")

    def test_duplicate_unavailable_combinations_are_rejected_atomically(self):
        manifest = make_manifest(agent_id="combination-agent")
        combination = UnavailableCombination(
            agent_id="combination-agent",
            runtime="github_actions",
            reason_code="runtime_not_supported",
            explanation="Not supported.",
        )
        with self.assertRaisesRegex(RegistryConflictError, "duplicate unavailable"):
            self.registry.register_manifest(
                manifest, unavailable=(combination, combination),
            )
        with self.assertRaises(KeyError):
            self.registry.get("combination-agent")

    def test_registry_has_no_dispatch_operations_and_register_calls_only_describe(self):
        class RecordingAdapter:
            def __init__(self, manifest):
                self.manifest = manifest
                self.calls = []

            def describe(self):
                self.calls.append("describe")
                return self.manifest

            def preflight(self, task, grant):
                self.calls.append("preflight")
                return PreflightResult(True)

            def dispatch(self, task, grant):
                self.calls.append("dispatch")
                raise AssertionError("shadow registration must not dispatch")

            def status(self, receipt):
                self.calls.append("status")
                raise AssertionError("shadow registration must not poll")

            def cancel(self, receipt, reason):
                self.calls.append("cancel")
                raise AssertionError("shadow registration must not cancel")

            def collect(self, receipt):
                self.calls.append("collect")
                raise AssertionError("shadow registration must not collect")

            def recover(self, task, attempt_id):
                self.calls.append("recover")
                raise AssertionError("shadow registration must not recover")

        adapter = RecordingAdapter(make_manifest(agent_id="recording-agent"))
        self.assertIsInstance(adapter, AgentAdapter)
        self.registry.register_adapter(adapter)
        self.assertEqual(adapter.calls, ["describe"])
        for operation in ("dispatch", "status", "cancel", "collect", "recover", "preflight"):
            self.assertFalse(hasattr(self.registry, operation))
        self.assertEqual(self.registry.operational_agents, ())

    def test_serialised_contracts_contain_references_not_credentials(self):
        task = make_task()
        payloads = {
            "registry": self.registry.canonical_json(),
            "task": task.canonical_json(),
            "grant": make_grant(task).canonical_json(),
            "report": make_report(task).canonical_json(),
            "verification": make_verification(task).canonical_json(),
        }
        self.assertIn("credential_refs", payloads["registry"])
        self.assertIn("reference", payloads["registry"])
        for name, serialised in payloads.items():
            for pattern in (
                r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{8,}",
                r"ghp_[A-Za-z0-9]{8,}",
                r"AKIA[0-9A-Z]{8,}",
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                r"(?i)password\s*=",
            ):
                with self.subTest(payload=name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, serialised))
        with self.assertRaises(ContractValidationError):
            CredentialReference("ghp_thisLooksLikeARealToken")


class CompatibilityTests(unittest.TestCase):
    def test_contracts_are_additive_to_existing_worker_contract(self):
        worker = Worker("ina", ("codex",))
        self.assertEqual(worker.adapter, "codex")
        self.assertEqual(worker.name, "ina")
        package = Path(__file__).resolve().parents[1] / "orchestra" / "contracts.py"
        self.assertTrue(package.is_file())


if __name__ == "__main__":
    unittest.main()
