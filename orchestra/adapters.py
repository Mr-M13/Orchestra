"""Provider-independent adapter interface and shadow-mode registry.

The registry is a catalogue, not an execution interface.  It can describe
configured agents and answer capability/runtime questions.  It intentionally
does not expose ``dispatch``, ``status``, ``cancel``, ``collect`` or ``recover``
operations, so registering an agent cannot take over execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from orchestra.contracts import (
    CAPABILITIES,
    EXECUTION_RUNTIMES,
    AgentManifest,
    CapabilityClaim,
    Concurrency,
    ContractValidationError,
    CostCharacteristics,
    CredentialReference,
    ExecutionGrant,
    ExecutionReport,
    IsolationRequirements,
    NetworkPolicy,
    RegistryConflictError,
    ResourceLimits,
    TaskSpec,
    ToolRequirement,
    _agent_id,
    _identifier,
    _model_id,
    _optional_text,
    _text,
    _timestamp,
    canonical_json,
)


ADAPTER_STATES = frozenset({
    "accepted",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
})
RECOVERY_STATES = frozenset({
    "recovered",
    "not_found",
    "ambiguous",
    "failed",
})


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ContractValidationError("preflight ok must be a boolean")
        if not isinstance(self.reasons, (tuple, list)) or not all(
            isinstance(reason, str) and reason for reason in self.reasons
        ):
            raise ContractValidationError("preflight reasons must be non-empty strings")
        if not self.ok and not self.reasons:
            raise ContractValidationError("failed preflight requires a reason")
        if not isinstance(self.checks, (tuple, list)) or not all(
            isinstance(check, str) and check for check in self.checks
        ):
            raise ContractValidationError("preflight checks must be non-empty strings")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "checks", tuple(self.checks))

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "checks": list(self.checks),
        }


@dataclass(frozen=True)
class DispatchReceipt:
    attempt_id: str
    provider_run_id: str
    adapter_id: str
    runtime: str
    dispatched_at: str

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.provider_run_id, "provider_run_id")
        _identifier(self.adapter_id, "adapter_id")
        if self.runtime not in EXECUTION_RUNTIMES:
            raise ContractValidationError("unknown dispatch runtime")
        object.__setattr__(
            self, "dispatched_at", _timestamp(self.dispatched_at, "dispatched_at"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "attempt_id": self.attempt_id,
            "provider_run_id": self.provider_run_id,
            "adapter_id": self.adapter_id,
            "runtime": self.runtime,
            "dispatched_at": self.dispatched_at,
        }


@dataclass(frozen=True)
class AdapterStatus:
    state: str
    provider_state: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state not in ADAPTER_STATES:
            raise ContractValidationError("unknown adapter status state")
        if self.provider_state is not None:
            _identifier(self.provider_state, "provider_state")
        object.__setattr__(
            self, "detail", _optional_text(self.detail, "status detail", maximum=2_000),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "provider_state": self.provider_state,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CancellationResult:
    requested: bool
    provider_state: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.requested, bool):
            raise ContractValidationError("cancellation requested must be a boolean")
        if self.provider_state is not None:
            _identifier(self.provider_state, "provider_state")
        object.__setattr__(
            self,
            "detail",
            _optional_text(self.detail, "cancellation detail", maximum=2_000),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "provider_state": self.provider_state,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RecoveryResult:
    state: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state not in RECOVERY_STATES:
            raise ContractValidationError("unknown recovery state")
        object.__setattr__(
            self, "detail", _optional_text(self.detail, "recovery detail", maximum=2_000),
        )

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state, "detail": self.detail}


@runtime_checkable
class AgentAdapter(Protocol):
    """The seven-operation contract every runtime adapter must satisfy.

    Implementations are expected to be fail-closed.  ``describe`` and
    ``preflight`` must not create external side effects.  ``dispatch`` may only
    run after an independent execution authority has validated a grant.
    """

    def describe(self) -> AgentManifest:
        ...

    def preflight(self, task: TaskSpec, grant: ExecutionGrant) -> PreflightResult:
        ...

    def dispatch(self, task: TaskSpec, grant: ExecutionGrant) -> DispatchReceipt:
        ...

    def status(self, receipt: DispatchReceipt) -> AdapterStatus:
        ...

    def cancel(self, receipt: DispatchReceipt, reason: str) -> CancellationResult:
        ...

    def collect(self, receipt: DispatchReceipt) -> ExecutionReport:
        ...

    def recover(self, task: TaskSpec, attempt_id: str) -> RecoveryResult:
        ...


@dataclass(frozen=True)
class UnavailableCombination:
    """A known agent/runtime/capability combination that must not be selected."""

    agent_id: str
    reason_code: str
    explanation: str
    runtime: str | None = None
    capability: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        _agent_id(self.agent_id)
        _identifier(self.reason_code, "reason_code")
        _text(self.explanation, "explanation", maximum=1_000)
        if self.runtime is not None and self.runtime not in EXECUTION_RUNTIMES:
            raise ContractValidationError("unavailable combination has unknown runtime")
        if self.capability is not None and self.capability not in CAPABILITIES:
            raise ContractValidationError("unavailable combination has unknown capability")
        if self.model_id is not None:
            _model_id(self.model_id, "model_id")
        if self.runtime is None and self.capability is None and self.model_id is None:
            raise ContractValidationError(
                "unavailable combination must constrain runtime, capability or model"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "runtime": self.runtime,
            "capability": self.capability,
            "model_id": self.model_id,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class AgentAvailability:
    agent_id: str
    runtime: str | None
    capability: str | None
    available: bool
    reasons: tuple[str, ...] = ()
    model_id: str | None = None
    coding_harness: str | None = None

    def __post_init__(self) -> None:
        _agent_id(self.agent_id)
        if self.runtime is not None and self.runtime not in EXECUTION_RUNTIMES:
            raise ContractValidationError("availability has unknown runtime")
        if self.capability is not None and self.capability not in CAPABILITIES:
            raise ContractValidationError("availability has unknown capability")
        if self.model_id is not None:
            _model_id(self.model_id, "model_id")
        if self.coding_harness is not None:
            _identifier(self.coding_harness, "coding_harness")
        if not isinstance(self.available, bool):
            raise ContractValidationError("availability must be a boolean")
        if not self.available and not self.reasons:
            raise ContractValidationError(
                "unavailable combinations require at least one reason"
            )
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "runtime": self.runtime,
            "capability": self.capability,
            "model_id": self.model_id,
            "coding_harness": self.coding_harness,
            "available": self.available,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class AdapterRegistration:
    manifest: AgentManifest
    configured: bool = True
    operational: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractValidationError("unsupported adapter registration schema")
        if not isinstance(self.manifest, AgentManifest):
            raise ContractValidationError("registration requires an AgentManifest")
        if not isinstance(self.configured, bool) or not isinstance(self.operational, bool):
            raise ContractValidationError("registration flags must be booleans")
        if self.operational and not self.configured:
            raise ContractValidationError("an unconfigured adapter cannot be operational")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest": self.manifest.to_dict(),
            "configured": self.configured,
            "operational": self.operational,
        }


class ShadowAdapterRegistry:
    """A catalogue of configured agents with no execution authority.

    The registry never calls an adapter's ``preflight``, ``dispatch``,
    ``status``, ``cancel``, ``collect`` or ``recover`` methods.  Registration
    uses only ``describe``.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, AdapterRegistration] = {}
        self._unavailable: list[UnavailableCombination] = []

    def register_manifest(
        self,
        manifest: AgentManifest,
        unavailable: tuple[UnavailableCombination, ...] = (),
        *,
        configured: bool = True,
        operational: bool = False,
    ) -> AdapterRegistration:
        if not isinstance(manifest, AgentManifest):
            raise ContractValidationError("register_manifest requires an AgentManifest")
        if manifest.agent_id in self._registrations:
            raise RegistryConflictError(
                f"duplicate agent id: {manifest.agent_id}"
            )

        entries = tuple(unavailable)
        if not all(isinstance(item, UnavailableCombination) for item in entries):
            raise ContractValidationError(
                "unavailable must contain UnavailableCombination values"
            )
        for entry in entries:
            if entry.agent_id != manifest.agent_id:
                raise RegistryConflictError(
                    "unavailable combination names a different agent"
                )

        existing_keys = {
            (item.agent_id, item.runtime, item.capability, item.model_id)
            for item in self._unavailable
        }
        new_keys: set[tuple[str, str | None, str | None, str | None]] = set()
        for entry in entries:
            key = (entry.agent_id, entry.runtime, entry.capability, entry.model_id)
            if key in existing_keys or key in new_keys:
                raise RegistryConflictError(
                    "duplicate unavailable combination"
                )
            new_keys.add(key)

        registration = AdapterRegistration(
            manifest=manifest,
            configured=configured,
            operational=operational,
        )
        # Commit only after all validation succeeds.
        self._registrations[manifest.agent_id] = registration
        self._unavailable.extend(entries)
        return registration

    def register_adapter(
        self,
        adapter: AgentAdapter,
        unavailable: tuple[UnavailableCombination, ...] = (),
        *,
        configured: bool = True,
        operational: bool = False,
    ) -> AdapterRegistration:
        """Describe and register an adapter without invoking execution methods."""

        describe = getattr(adapter, "describe", None)
        if not callable(describe):
            raise ContractValidationError("adapter must implement describe()")
        manifest = describe()
        if not isinstance(manifest, AgentManifest):
            raise ContractValidationError("adapter describe() must return an AgentManifest")
        return self.register_manifest(
            manifest,
            unavailable,
            configured=configured,
            operational=operational,
        )

    def registration(self, agent_id: str) -> AdapterRegistration:
        try:
            return self._registrations[agent_id]
        except KeyError:
            raise KeyError("unknown agent") from None

    def registrations(self) -> tuple[AdapterRegistration, ...]:
        return tuple(
            self._registrations[agent_id]
            for agent_id in sorted(self._registrations)
        )

    @property
    def agents(self) -> tuple[AgentManifest, ...]:
        return tuple(
            registration.manifest for registration in self.registrations()
        )

    @property
    def configured_agents(self) -> tuple[str, ...]:
        return tuple(
            registration.manifest.agent_id
            for registration in self.registrations()
            if registration.configured
        )

    @property
    def operational_agents(self) -> tuple[str, ...]:
        return tuple(
            registration.manifest.agent_id
            for registration in self.registrations()
            if registration.operational
        )

    def get(self, agent_id: str) -> AgentManifest:
        return self.registration(agent_id).manifest

    def capabilities(self, agent_id: str) -> tuple[CapabilityClaim, ...]:
        return self.get(agent_id).capabilities

    def verified_capabilities(self, agent_id: str) -> tuple[str, ...]:
        return self.get(agent_id).verified_capabilities

    def unverified_capabilities(self, agent_id: str) -> tuple[str, ...]:
        return self.get(agent_id).unverified_capabilities

    def runtimes(self, agent_id: str) -> tuple[str, ...]:
        return self.get(agent_id).supported_runtimes

    @property
    def unavailable_combinations(self) -> tuple[UnavailableCombination, ...]:
        return tuple(
            sorted(
                self._unavailable,
                key=lambda item: (
                    item.agent_id,
                    item.runtime or "",
                    item.capability or "",
                    item.model_id or "",
                ),
            )
        )

    def availability(
        self,
        agent_id: str,
        *,
        runtime: str | None = None,
        capability: str | None = None,
        model_id: str | None = None,
        coding_harness: str | None = None,
    ) -> AgentAvailability:
        manifest = self.get(agent_id)
        if runtime is not None and runtime not in EXECUTION_RUNTIMES:
            raise ContractValidationError("unknown runtime")
        if capability is not None and capability not in CAPABILITIES:
            raise ContractValidationError("unknown capability")
        if model_id is not None:
            _model_id(model_id, "model_id")
        if coding_harness is not None:
            _identifier(coding_harness, "coding_harness")

        reasons: list[str] = []
        if runtime is not None and not manifest.supports_runtime(runtime):
            reasons.append("runtime_not_supported")
        if capability is not None and manifest.capability_claim(capability) is None:
            reasons.append("capability_not_claimed")
        if model_id is not None and model_id not in manifest.model_ids:
            reasons.append("model_not_supported")
        if (
            coding_harness is not None
            and coding_harness != manifest.coding_harness
        ):
            reasons.append("harness_not_supported")

        for entry in self._unavailable:
            if entry.agent_id != agent_id:
                continue
            if entry.runtime is not None and entry.runtime != runtime:
                continue
            if entry.capability is not None and entry.capability != capability:
                continue
            if entry.model_id is not None and entry.model_id != model_id:
                continue
            if entry.reason_code not in reasons:
                reasons.append(entry.reason_code)

        return AgentAvailability(
            agent_id=agent_id,
            runtime=runtime,
            capability=capability,
            model_id=model_id,
            coding_harness=coding_harness,
            available=not reasons,
            reasons=tuple(reasons),
        )

    def catalogue(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "shadow_mode": True,
            "takes_over_execution": False,
            "registrations": [
                registration.to_dict()
                for registration in self.registrations()
            ],
            "agent_classes": {
                "coding": sorted(
                    manifest.agent_id for manifest in self.agents
                    if manifest.agent_class == "coding"
                ),
                "response_only": sorted(
                    manifest.agent_id for manifest in self.agents
                    if manifest.agent_class == "response_only"
                ),
            },
            "unavailable_combinations": [
                entry.to_dict() for entry in self.unavailable_combinations
            ],
        }

    def canonical_json(self) -> str:
        return canonical_json(self.catalogue())


def _coding_claims(*capabilities: str) -> tuple[CapabilityClaim, ...]:
    return tuple(CapabilityClaim(capability) for capability in capabilities)


def default_shadow_registry() -> ShadowAdapterRegistry:
    """Describe the adapters that exist today without changing execution."""

    registry = ShadowAdapterRegistry()

    registry.register_manifest(
        AgentManifest(
            agent_id="hamed-ina-codex-local",
            display_name="Hamed/Ina Codex local",
            agent_version="configured-v1",
            agent_class="coding",
            model_provider="openai",
            model_ids=("gpt-5-codex",),
            coding_harness="codex-cli",
            supported_runtimes=("local_process",),
            capabilities=_coding_claims(
                "read_repository",
                "edit_workspace",
                "run_commands",
                "run_tests",
                "produce_patch",
            ),
            tool_requirements=(
                ToolRequirement("codex-cli"),
                ToolRequirement("git"),
            ),
            isolation=IsolationRequirements(
                filesystem="workspace_only",
                credentials="owner_environment",
                git_metadata="read_only",
                sandbox="required",
            ),
            network=NetworkPolicy("denied"),
            credential_refs=(CredentialReference("file:codex-home"),),
            resource_limits=ResourceLimits(
                max_runtime_seconds=1_800,
                max_output_bytes=65_536,
            ),
            concurrency=Concurrency(max_parallel_attempts=1),
            cost=CostCharacteristics(cost_class="subscription"),
            notes=(
                "Shadow description of the existing PixelBox local Codex lanes. "
                "The deployed configuration uses separate Ina and Hamed Codex homes."
            ),
        ),
        unavailable=(
            UnavailableCombination(
                agent_id="hamed-ina-codex-local",
                runtime="github_actions",
                reason_code="runtime_not_supported",
                explanation="This adapter describes the local process runtime only.",
            ),
        ),
    )

    registry.register_manifest(
        AgentManifest(
            agent_id="codex-github-actions",
            display_name="Codex GitHub Actions",
            agent_version="configured-v1",
            agent_class="coding",
            model_provider="openai",
            model_ids=("gpt-5-codex",),
            coding_harness="codex-cli",
            supported_runtimes=("github_actions",),
            capabilities=_coding_claims(
                "read_repository",
                "edit_workspace",
                "run_commands",
                "run_tests",
                "produce_patch",
            ),
            tool_requirements=(
                ToolRequirement("codex-cli"),
                ToolRequirement("git"),
                ToolRequirement("github-actions"),
            ),
            isolation=IsolationRequirements(
                filesystem="ephemeral_workspace",
                credentials="runner_scoped",
                git_metadata="read_only",
                sandbox="ephemeral_runner",
            ),
            network=NetworkPolicy("unrestricted"),
            credential_refs=(CredentialReference("broker:actions-codex"),),
            resource_limits=ResourceLimits(
                max_runtime_seconds=10_800,
                max_output_bytes=65_536,
            ),
            concurrency=Concurrency(max_parallel_attempts=1),
            cost=CostCharacteristics(cost_class="metered"),
            notes=(
                "Shadow description of the implemented remote Actions Codex path; "
                "it is not a configured AI-worker backend on the current local runtime."
            ),
        ),
        unavailable=(
            UnavailableCombination(
                agent_id="codex-github-actions",
                runtime="local_process",
                reason_code="runtime_not_supported",
                explanation="This adapter describes the GitHub Actions runtime only.",
            ),
        ),
        configured=False,
    )

    registry.register_manifest(
        AgentManifest(
            agent_id="deepseek-opencode-github-actions",
            display_name="DeepSeek/OpenCode GitHub Actions",
            agent_version="configured-v1",
            agent_class="coding",
            model_provider="deepseek",
            model_ids=("deepseek/deepseek-chat",),
            coding_harness="opencode",
            supported_runtimes=("github_actions",),
            capabilities=_coding_claims(
                "read_repository",
                "edit_workspace",
                "run_commands",
                "run_tests",
                "produce_patch",
            ),
            tool_requirements=(
                ToolRequirement("opencode"),
                ToolRequirement("git"),
                ToolRequirement("github-actions"),
            ),
            isolation=IsolationRequirements(
                filesystem="ephemeral_workspace",
                credentials="runner_scoped",
                git_metadata="read_only",
                sandbox="ephemeral_runner",
            ),
            network=NetworkPolicy("unrestricted"),
            credential_refs=(CredentialReference("broker:actions-deepseek"),),
            resource_limits=ResourceLimits(
                max_runtime_seconds=10_800,
                max_output_bytes=65_536,
            ),
            concurrency=Concurrency(max_parallel_attempts=1),
            cost=CostCharacteristics(cost_class="metered"),
            notes=(
                "Shadow description of the implemented remote Actions OpenCode path; "
                "it is not configured as an AI-worker backend and is not enabled as "
                "a new coding worker by this registry."
            ),
        ),
        unavailable=(
            UnavailableCombination(
                agent_id="deepseek-opencode-github-actions",
                runtime="local_process",
                reason_code="runtime_not_supported",
                explanation="This adapter describes the GitHub Actions runtime only.",
            ),
        ),
        configured=False,
    )

    registry.register_manifest(
        AgentManifest(
            agent_id="deepseek-direct-response",
            display_name="DeepSeek direct response-only",
            agent_version="configured-v1",
            agent_class="response_only",
            model_provider="deepseek",
            model_ids=("deepseek/deepseek-chat",),
            coding_harness="none",
            supported_runtimes=("provider_api",),
            capabilities=(CapabilityClaim("respond"),),
            tool_requirements=(),
            isolation=IsolationRequirements(
                filesystem="none",
                credentials="broker_only",
                git_metadata="none",
                sandbox="provider_builtin",
            ),
            network=NetworkPolicy(
                "allowlisted", allowed_hosts=("api.deepseek.com",),
            ),
            credential_refs=(CredentialReference("broker:deepseek-provider"),),
            resource_limits=ResourceLimits(
                max_runtime_seconds=1_800,
                max_output_bytes=65_536,
            ),
            concurrency=Concurrency(max_parallel_attempts=1),
            cost=CostCharacteristics(cost_class="metered"),
            notes=(
                "Response-only adapter: it may answer text, but it has no "
                "workspace, Git or verification capability and is not a coding worker."
            ),
        ),
        unavailable=(
            UnavailableCombination(
                agent_id="deepseek-direct-response",
                runtime="github_actions",
                reason_code="response_only_runtime_not_supported",
                explanation="The direct response adapter cannot use a coding runtime.",
            ),
            UnavailableCombination(
                agent_id="deepseek-direct-response",
                runtime="local_process",
                reason_code="response_only_runtime_not_supported",
                explanation="The direct response adapter cannot use a local worker.",
            ),
            UnavailableCombination(
                agent_id="deepseek-direct-response",
                capability="edit_workspace",
                reason_code="response_only_no_coding",
                explanation="A response-only adapter must not be selected to edit a workspace.",
            ),
        ),
    )

    return registry
