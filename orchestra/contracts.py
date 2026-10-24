"""Shared, provider-independent contracts for Orchestra.

The contracts in this module are intentionally descriptive.  They do not
authorise execution, issue credentials, sign grants or perform verification.
The only implementation provided here is structural validation, deterministic
canonicalisation and digest generation.

Security invariant: a worker report is untrusted input.  A successful report
never becomes verified completion without a separate :class:`VerificationRecord`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1

CONSEQUENCE_CLASSES = frozenset({
    "routine_development",
    "production_deployment",
    "destructive_operation",
    "security_change",
    "unclassified",
})
SENSITIVE_CONSEQUENCE_CLASSES = frozenset({
    "production_deployment",
    "destructive_operation",
    "security_change",
})
APPROVAL_CLASSES = frozenset({"routine", "sensitive", "unclassified"})

EXECUTION_RUNTIMES = frozenset({
    "local_process",
    "github_actions",
    "provider_api",
})

AGENT_CLASSES = frozenset({"coding", "response_only"})

CAPABILITIES = frozenset({
    "respond",
    "read_repository",
    "edit_workspace",
    "run_commands",
    "run_tests",
    "produce_patch",
    "create_branch",
    "open_pull_request",
    "review_code",
})
CODING_CAPABILITIES = CAPABILITIES - {"respond"}

AUTHORIZED_OPERATIONS = frozenset({
    "read_repository",
    "edit_workspace",
    "run_commands",
    "run_tests",
    "write_artifacts",
    "create_branch",
    "push_branch",
    "open_pull_request",
    "merge",
    "deploy",
    "access_network",
    "use_credentials",
})
WRITE_OPERATIONS = frozenset({"edit_workspace", "write_artifacts"})

VERIFICATION_STATUSES = frozenset({"verified", "unverified", "rejected", "stale"})
VERIFICATION_RESULTS = frozenset({
    "pass",
    "fail",
    "inconclusive",
    "not_run",
    "blocked",
    "invalid",
    "stale",
})
EXECUTION_OUTCOMES = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
    "unknown",
})
TEST_EVIDENCE_STATUSES = frozenset({
    "passed",
    "failed",
    "skipped",
    "not_run",
    "blocked",
})

FILESYSTEM_ISOLATION = frozenset({
    "none",
    "workspace_only",
    "ephemeral_workspace",
    "host",
})
NETWORK_MODES = frozenset({"denied", "allowlisted", "unrestricted"})
CREDENTIAL_ISOLATION = frozenset({
    "none",
    "broker_only",
    "runner_scoped",
    "owner_environment",
})
GIT_METADATA_ISOLATION = frozenset({"none", "read_only", "writable"})
SANDBOX_ISOLATION = frozenset({
    "required",
    "provider_builtin",
    "ephemeral_runner",
    "none",
})

COST_CLASSES = frozenset({"free", "subscription", "metered", "unknown"})

UNTRUSTED_REPORT_TRUST = "untrusted_worker_report"
UNSIGNED_GRANT_INTEGRITY = "unsigned_contract_only"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_HARNESS_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PATH_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?$"
)
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CREDENTIAL_REFERENCE = re.compile(
    r"^(?:env|file|keychain|broker|secret):[A-Za-z0-9_.:@/-]{1,160}$"
)
_IMMUTABLE_INSTRUCTION_REFERENCE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|urn:orchestra:[A-Za-z0-9_.:/-]{1,200})$"
)
_URI_REFERENCE = re.compile(r"^[a-z][a-z0-9+.-]*:[^\s\x00]{1,2048}$")
_PULL_REQUEST = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]{0,9}$"
)
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class ContractValidationError(ValueError):
    """A contract violates a structural or security invariant."""


class GrantValidationError(ContractValidationError):
    """An execution grant cannot be bound to a task or attempt."""


class RegistryConflictError(ContractValidationError):
    """An adapter registry entry would duplicate or contradict another entry."""


def canonical_json(value: Any) -> str:
    """Serialise JSON deterministically.

    Mapping keys are sorted, insignificant whitespace is removed and non-finite
    numbers are rejected.  Sequence order is preserved; contracts that treat a
    collection as unordered sort it during validation.
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _text(value: Any, field_name: str, *, minimum: int = 1,
          maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{field_name} must be a string")
    if "\x00" in value:
        raise ContractValidationError(f"{field_name} must not contain NUL")
    if not minimum <= len(value) <= maximum:
        raise ContractValidationError(
            f"{field_name} must be between {minimum} and {maximum} characters"
        )
    return value


def _optional_text(value: Any, field_name: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum=maximum)


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContractValidationError(f"{field_name} is not a safe identifier")
    return value


def _agent_id(value: Any, field_name: str = "agent_id") -> str:
    if not isinstance(value, str) or not _AGENT_ID.fullmatch(value):
        raise ContractValidationError(f"{field_name} is not a safe agent id")
    return value


def _provider_id(value: Any, field_name: str = "model_provider") -> str:
    if not isinstance(value, str) or not _PROVIDER_ID.fullmatch(value):
        raise ContractValidationError(f"{field_name} is not a safe provider id")
    return value


def _model_id(value: Any, field_name: str = "model_id") -> str:
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise ContractValidationError(f"{field_name} is not a safe model id")
    return value


def _harness_id(value: Any, field_name: str = "coding_harness") -> str:
    if not isinstance(value, str) or not _HARNESS_ID.fullmatch(value):
        raise ContractValidationError(f"{field_name} is not a safe harness id")
    return value


def _version(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ContractValidationError(f"{field_name} is not a safe version")
    return value


def _repository(value: Any) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ContractValidationError("repository must be owner/name")
    return value


def _commit(value: Any, field_name: str = "base_commit") -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ContractValidationError(
            f"{field_name} must be a full lowercase 40-character Git commit"
        )
    return value


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContractValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _bounded_integer(value: Any, field_name: str, *, minimum: int,
                     maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise ContractValidationError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def _non_negative_number(value: Any, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractValidationError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ContractValidationError(f"{field_name} must be finite and non-negative")
    return number


def _string_tuple(
    values: Any,
    field_name: str,
    *,
    allowed: frozenset[str] | None = None,
    pattern: re.Pattern[str] | None = None,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ContractValidationError(f"{field_name} must be a tuple or list")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ContractValidationError(f"{field_name} contains an invalid value")
        if pattern is not None and not pattern.fullmatch(value):
            raise ContractValidationError(f"{field_name} contains an unsafe value")
        if allowed is not None and value not in allowed:
            raise ContractValidationError(f"{field_name} contains an unknown value: {value}")
        if value in result:
            raise ContractValidationError(f"{field_name} contains duplicates")
        result.append(value)
    if not result and not allow_empty:
        raise ContractValidationError(f"{field_name} must not be empty")
    return tuple(sorted(result))


def _paths(values: Any, field_name: str, *, writable: bool) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ContractValidationError(f"{field_name} must be a tuple or list")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ContractValidationError(f"{field_name} contains an invalid path")
        if value.startswith("/") or value.startswith("~"):
            raise ContractValidationError(f"{field_name} must contain relative paths")
        normalised = value.rstrip("/")
        if not normalised:
            raise ContractValidationError(f"{field_name} contains an invalid path")
        if normalised == ".":
            if writable:
                raise ContractValidationError(
                    f"{field_name} cannot grant writes to the workspace root"
                )
        else:
            parts = normalised.split("/")
            if any(not _PATH_PART.fullmatch(part) or part in {".", ".."}
                   for part in parts):
                raise ContractValidationError(f"{field_name} contains an unsafe path")
            if writable and parts[0] in {".git", ".codex"}:
                raise ContractValidationError(
                    f"{field_name} cannot grant writes to protected metadata"
                )
        if normalised in result:
            raise ContractValidationError(f"{field_name} contains duplicates")
        result.append(normalised)
    return tuple(sorted(result))


def _hosts(values: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ContractValidationError(f"{field_name} must be a tuple or list")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ContractValidationError(f"{field_name} contains an invalid host")
        normalised = value.strip().lower()
        if not _HOST.fullmatch(normalised):
            raise ContractValidationError(f"{field_name} contains an unsafe host")
        if normalised in result:
            raise ContractValidationError(f"{field_name} contains duplicates")
        result.append(normalised)
    return tuple(sorted(result))


def _timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractValidationError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_timestamp(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, field_name)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _coerce_now(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    elif isinstance(now, datetime):
        parsed = now
    else:
        raise GrantValidationError("now must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GrantValidationError("now must include a timezone")
    return parsed.astimezone(timezone.utc)


def _uri(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _URI_REFERENCE.fullmatch(value):
        raise ContractValidationError(f"{field_name} must be a bounded URI reference")
    return value


@dataclass(frozen=True)
class CredentialReference:
    """A non-secret reference to a credential held elsewhere."""

    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not _CREDENTIAL_REFERENCE.fullmatch(
            self.reference
        ):
            raise ContractValidationError(
                "credential reference must use env:, file:, keychain:, broker: or secret: "
                "and must not contain a secret value"
            )

    def to_dict(self) -> dict[str, str]:
        return {"reference": self.reference}


@dataclass(frozen=True)
class ToolRequirement:
    name: str
    required: bool = True
    reference: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, "tool name")
        if not isinstance(self.required, bool):
            raise ContractValidationError("tool required must be a boolean")
        if self.reference is not None:
            _uri(self.reference, "tool reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "reference": self.reference,
        }


@dataclass(frozen=True)
class CapabilityClaim:
    capability: str
    verification: str = "unverified"
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ContractValidationError(
                f"unknown capability: {self.capability}"
            )
        if self.verification not in VERIFICATION_STATUSES:
            raise ContractValidationError(
                f"unknown capability verification status: {self.verification}"
            )
        if self.evidence_ref is not None:
            _uri(self.evidence_ref, "capability evidence_ref")
        if self.verification == "verified" and self.evidence_ref is None:
            raise ContractValidationError(
                "verified capabilities require an evidence reference"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "verification": self.verification,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True)
class IsolationRequirements:
    filesystem: str = "workspace_only"
    credentials: str = "broker_only"
    git_metadata: str = "read_only"
    sandbox: str = "required"

    def __post_init__(self) -> None:
        if self.filesystem not in FILESYSTEM_ISOLATION:
            raise ContractValidationError("unknown filesystem isolation requirement")
        if self.credentials not in CREDENTIAL_ISOLATION:
            raise ContractValidationError("unknown credential isolation requirement")
        if self.git_metadata not in GIT_METADATA_ISOLATION:
            raise ContractValidationError("unknown git metadata isolation requirement")
        if self.sandbox not in SANDBOX_ISOLATION:
            raise ContractValidationError("unknown sandbox isolation requirement")

    def to_dict(self) -> dict[str, str]:
        return {
            "filesystem": self.filesystem,
            "credentials": self.credentials,
            "git_metadata": self.git_metadata,
            "sandbox": self.sandbox,
        }


@dataclass(frozen=True)
class NetworkPolicy:
    mode: str = "denied"
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in NETWORK_MODES:
            raise ContractValidationError("unknown network mode")
        hosts = _hosts(self.allowed_hosts, "allowed_hosts")
        if self.mode == "allowlisted" and not hosts:
            raise ContractValidationError(
                "allowlisted network policy requires at least one host"
            )
        if self.mode != "allowlisted" and hosts:
            raise ContractValidationError(
                "only an allowlisted network policy may name hosts"
            )
        object.__setattr__(self, "allowed_hosts", hosts)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "allowed_hosts": list(self.allowed_hosts)}


@dataclass(frozen=True)
class ResourceLimits:
    max_runtime_seconds: int = 1_800
    max_memory_mb: int | None = None
    max_disk_mb: int | None = None
    max_output_bytes: int = 65_536

    def __post_init__(self) -> None:
        _bounded_integer(
            self.max_runtime_seconds, "max_runtime_seconds",
            minimum=1, maximum=604_800,
        )
        _bounded_integer(
            self.max_output_bytes, "max_output_bytes",
            minimum=1, maximum=1_048_576,
        )
        if self.max_memory_mb is not None:
            _bounded_integer(
                self.max_memory_mb, "max_memory_mb",
                minimum=1, maximum=1_048_576,
            )
        if self.max_disk_mb is not None:
            _bounded_integer(
                self.max_disk_mb, "max_disk_mb",
                minimum=1, maximum=10_485_760,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_memory_mb": self.max_memory_mb,
            "max_disk_mb": self.max_disk_mb,
            "max_output_bytes": self.max_output_bytes,
        }


@dataclass(frozen=True)
class Concurrency:
    max_parallel_attempts: int = 1
    exclusive_worker: bool = True

    def __post_init__(self) -> None:
        _bounded_integer(
            self.max_parallel_attempts, "max_parallel_attempts",
            minimum=1, maximum=64,
        )
        if not isinstance(self.exclusive_worker, bool):
            raise ContractValidationError("exclusive_worker must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_parallel_attempts": self.max_parallel_attempts,
            "exclusive_worker": self.exclusive_worker,
        }


@dataclass(frozen=True)
class CostCharacteristics:
    cost_class: str = "unknown"
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    billing_reference: str | None = None

    def __post_init__(self) -> None:
        if self.cost_class not in COST_CLASSES:
            raise ContractValidationError("unknown cost class")
        if self.input_usd_per_million is not None:
            object.__setattr__(
                self, "input_usd_per_million",
                _non_negative_number(
                    self.input_usd_per_million, "input_usd_per_million",
                ),
            )
        if self.output_usd_per_million is not None:
            object.__setattr__(
                self, "output_usd_per_million",
                _non_negative_number(
                    self.output_usd_per_million, "output_usd_per_million",
                ),
            )
        if self.billing_reference is not None:
            _identifier(self.billing_reference, "billing_reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_class": self.cost_class,
            "input_usd_per_million": self.input_usd_per_million,
            "output_usd_per_million": self.output_usd_per_million,
            "billing_reference": self.billing_reference,
        }


@dataclass(frozen=True)
class AgentManifest:
    """Declarative identity and capability description for one agent."""

    agent_id: str
    display_name: str
    agent_version: str
    agent_class: str
    model_provider: str
    model_ids: tuple[str, ...]
    coding_harness: str
    supported_runtimes: tuple[str, ...]
    capabilities: tuple[CapabilityClaim, ...]
    tool_requirements: tuple[ToolRequirement, ...] = ()
    isolation: IsolationRequirements = field(default_factory=IsolationRequirements)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    credential_refs: tuple[CredentialReference, ...] = ()
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    concurrency: Concurrency = field(default_factory=Concurrency)
    cost: CostCharacteristics = field(default_factory=CostCharacteristics)
    verification_status: str = "unverified"
    verification_evidence_ref: str | None = None
    notes: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported agent manifest schema version")
        _agent_id(self.agent_id)
        _text(self.display_name, "display_name", maximum=120)
        if "\n" in self.display_name or "\r" in self.display_name:
            raise ContractValidationError("display_name must be a single line")
        _version(self.agent_version, "agent_version")
        if self.agent_class not in AGENT_CLASSES:
            raise ContractValidationError("unknown agent class")
        _provider_id(self.model_provider)
        model_ids = _string_tuple(
            self.model_ids, "model_ids", pattern=_MODEL_ID,
        )
        object.__setattr__(self, "model_ids", model_ids)
        _harness_id(self.coding_harness)
        runtimes = _string_tuple(
            self.supported_runtimes, "supported_runtimes",
            allowed=EXECUTION_RUNTIMES,
        )
        object.__setattr__(self, "supported_runtimes", runtimes)

        if not isinstance(self.capabilities, (tuple, list)) or not self.capabilities:
            raise ContractValidationError("capabilities must contain at least one claim")
        claims: list[CapabilityClaim] = []
        seen: set[str] = set()
        for claim in self.capabilities:
            if not isinstance(claim, CapabilityClaim):
                raise ContractValidationError("capabilities must contain CapabilityClaim values")
            if claim.capability in seen:
                raise ContractValidationError(
                    f"duplicate capability claim: {claim.capability}"
                )
            seen.add(claim.capability)
            claims.append(claim)
        object.__setattr__(
            self, "capabilities", tuple(sorted(claims, key=lambda item: item.capability)),
        )

        if not isinstance(self.tool_requirements, (tuple, list)) or not all(
            isinstance(item, ToolRequirement) for item in self.tool_requirements
        ):
            raise ContractValidationError("tool_requirements must contain ToolRequirement values")
        tools = tuple(sorted(self.tool_requirements, key=lambda item: item.name))
        if len({item.name for item in tools}) != len(tools):
            raise ContractValidationError("tool_requirements contains duplicate names")
        object.__setattr__(self, "tool_requirements", tools)

        if not isinstance(self.isolation, IsolationRequirements):
            raise ContractValidationError("isolation must be IsolationRequirements")
        if not isinstance(self.network, NetworkPolicy):
            raise ContractValidationError("network must be NetworkPolicy")
        if not isinstance(self.credential_refs, (tuple, list)) or not all(
            isinstance(item, CredentialReference) for item in self.credential_refs
        ):
            raise ContractValidationError(
                "credential_refs must contain CredentialReference values"
            )
        references = tuple(sorted(
            self.credential_refs, key=lambda item: item.reference,
        ))
        if len({item.reference for item in references}) != len(references):
            raise ContractValidationError("credential_refs contains duplicates")
        object.__setattr__(self, "credential_refs", references)

        if not isinstance(self.resource_limits, ResourceLimits):
            raise ContractValidationError("resource_limits must be ResourceLimits")
        if not isinstance(self.concurrency, Concurrency):
            raise ContractValidationError("concurrency must be Concurrency")
        if not isinstance(self.cost, CostCharacteristics):
            raise ContractValidationError("cost must be CostCharacteristics")
        if self.verification_status not in VERIFICATION_STATUSES:
            raise ContractValidationError("unknown manifest verification status")
        if self.verification_evidence_ref is not None:
            _uri(self.verification_evidence_ref, "verification_evidence_ref")
        if self.verification_status == "verified":
            if self.verification_evidence_ref is None:
                raise ContractValidationError(
                    "verified manifests require a verification evidence reference"
                )
            if any(claim.verification != "verified" for claim in claims):
                raise ContractValidationError(
                    "a verified manifest may not contain unverified capabilities"
                )
        object.__setattr__(
            self, "notes", _optional_text(self.notes, "notes", maximum=2_000),
        )

        capability_names = {claim.capability for claim in claims}
        if self.agent_class == "response_only":
            if capability_names != {"respond"}:
                raise ContractValidationError(
                    "response-only agents may claim only the respond capability"
                )
            if self.coding_harness != "none":
                raise ContractValidationError(
                    "response-only agents must not declare a coding harness"
                )
            if tools:
                raise ContractValidationError(
                    "response-only agents must not require workspace tools"
                )
            if self.isolation.filesystem != "none":
                raise ContractValidationError(
                    "response-only agents must not request filesystem access"
                )
            if self.isolation.git_metadata != "none":
                raise ContractValidationError(
                    "response-only agents must not request Git metadata access"
                )
        else:
            if self.coding_harness == "none":
                raise ContractValidationError(
                    "coding agents must declare a coding harness"
                )
            if not capability_names & CODING_CAPABILITIES:
                raise ContractValidationError(
                    "coding agents must claim at least one coding capability"
                )
            if "provider_api" in runtimes:
                raise ContractValidationError(
                    "coding agents must use a workspace-bearing execution runtime"
                )

    @property
    def manifest_digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.canonical_payload()).encode("utf-8")
        ).hexdigest()

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "agent_version": self.agent_version,
            "agent_class": self.agent_class,
            "model_provider": self.model_provider,
            "model_ids": list(self.model_ids),
            "coding_harness": self.coding_harness,
            "supported_runtimes": list(self.supported_runtimes),
            "capabilities": [claim.to_dict() for claim in self.capabilities],
            "tool_requirements": [
                requirement.to_dict() for requirement in self.tool_requirements
            ],
            "isolation": self.isolation.to_dict(),
            "network": self.network.to_dict(),
            "credential_refs": [
                reference.to_dict() for reference in self.credential_refs
            ],
            "resource_limits": self.resource_limits.to_dict(),
            "concurrency": self.concurrency.to_dict(),
            "cost": self.cost.to_dict(),
            "verification_status": self.verification_status,
            "verification_evidence_ref": self.verification_evidence_ref,
            "notes": self.notes,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.canonical_payload(), "manifest_digest": self.manifest_digest}

    def canonical_json(self) -> str:
        return canonical_json(self.canonical_payload())

    @property
    def capability_names(self) -> tuple[str, ...]:
        return tuple(claim.capability for claim in self.capabilities)

    @property
    def verified_capabilities(self) -> tuple[str, ...]:
        return tuple(
            claim.capability for claim in self.capabilities
            if claim.verification == "verified"
        )

    @property
    def unverified_capabilities(self) -> tuple[str, ...]:
        return tuple(
            claim.capability for claim in self.capabilities
            if claim.verification != "verified"
        )

    def supports_runtime(self, runtime: str) -> bool:
        return runtime in self.supported_runtimes

    def capability_claim(self, capability: str) -> CapabilityClaim | None:
        return next(
            (claim for claim in self.capabilities if claim.capability == capability),
            None,
        )


@dataclass(frozen=True)
class FileScope:
    """Repository-relative paths a task may read or write."""

    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "read_paths", _paths(self.read_paths, "read_paths", writable=False),
        )
        object.__setattr__(
            self, "write_paths", _paths(self.write_paths, "write_paths", writable=True),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
        }


@dataclass(frozen=True)
class TaskSpec:
    """An immutable, canonical task proposal.

    ``digest`` binds every security-relevant field.  A grant or report that
    names a different digest cannot be treated as describing this task.
    """

    task_id: str
    request_id: str
    repository: str
    base_commit: str
    agent_id: str
    model_provider: str
    model_id: str
    coding_harness: str
    permitted_runtimes: tuple[str, ...]
    authorized_operations: tuple[str, ...]
    file_scope: FileScope
    network: NetworkPolicy
    isolation: IsolationRequirements
    resource_limits: ResourceLimits
    approval_class: str
    consequence_class: str
    instructions: str | None = None
    instruction_ref: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported task spec schema version")
        _identifier(self.task_id, "task_id")
        _identifier(self.request_id, "request_id")
        _repository(self.repository)
        _commit(self.base_commit)
        _agent_id(self.agent_id)
        _provider_id(self.model_provider)
        _model_id(self.model_id)
        _harness_id(self.coding_harness)
        object.__setattr__(
            self, "permitted_runtimes",
            _string_tuple(
                self.permitted_runtimes, "permitted_runtimes",
                allowed=EXECUTION_RUNTIMES,
            ),
        )
        object.__setattr__(
            self, "authorized_operations",
            _string_tuple(
                self.authorized_operations, "authorized_operations",
                allowed=AUTHORIZED_OPERATIONS,
            ),
        )
        if not isinstance(self.file_scope, FileScope):
            raise ContractValidationError("file_scope must be FileScope")
        if not isinstance(self.network, NetworkPolicy):
            raise ContractValidationError("network must be NetworkPolicy")
        if not isinstance(self.isolation, IsolationRequirements):
            raise ContractValidationError("isolation must be IsolationRequirements")
        if not isinstance(self.resource_limits, ResourceLimits):
            raise ContractValidationError("resource_limits must be ResourceLimits")
        if self.approval_class not in APPROVAL_CLASSES:
            raise ContractValidationError("unknown approval class")
        if self.consequence_class not in CONSEQUENCE_CLASSES:
            raise ContractValidationError("unknown consequence class")
        self._validate_approval_classification()
        self._validate_instructions()
        self._validate_scope_and_network()

    def _validate_approval_classification(self) -> None:
        if self.consequence_class in SENSITIVE_CONSEQUENCE_CLASSES:
            if self.approval_class != "sensitive":
                raise ContractValidationError(
                    "sensitive consequences require the sensitive approval class"
                )
        elif self.consequence_class == "routine_development":
            if self.approval_class not in {"routine", "sensitive"}:
                raise ContractValidationError(
                    "routine development may use routine or sensitive approval"
                )
        else:
            if self.approval_class != "unclassified":
                raise ContractValidationError(
                    "unclassified consequences require the unclassified approval class"
                )

    def _validate_instructions(self) -> None:
        if (self.instructions is None) == (self.instruction_ref is None):
            raise ContractValidationError(
                "task spec must contain exactly one of instructions or instruction_ref"
            )
        if self.instructions is not None:
            _text(self.instructions, "instructions", maximum=1_048_576)
            if not self.instructions.strip():
                raise ContractValidationError("instructions must not be blank")
        if self.instruction_ref is not None:
            if not isinstance(self.instruction_ref, str) or not (
                _IMMUTABLE_INSTRUCTION_REFERENCE.fullmatch(self.instruction_ref)
            ):
                raise ContractValidationError(
                    "instruction_ref must be an immutable sha256: or urn:orchestra: reference"
                )

    def _validate_scope_and_network(self) -> None:
        operations = set(self.authorized_operations)
        if "edit_workspace" in operations and not self.file_scope.write_paths:
            raise ContractValidationError(
                "edit_workspace requires at least one authorised write path"
            )
        if self.file_scope.write_paths and not operations & WRITE_OPERATIONS:
            raise ContractValidationError(
                "write paths require an authorised write operation"
            )
        if self.network.mode == "denied" and "access_network" in operations:
            raise ContractValidationError(
                "access_network conflicts with a denied network policy"
            )
        if self.network.mode != "denied" and "access_network" not in operations:
            raise ContractValidationError(
                "a network policy requires the access_network operation"
            )

    @property
    def instruction_digest(self) -> str:
        if self.instructions is not None:
            return hashlib.sha256(self.instructions.encode("utf-8")).hexdigest()
        assert self.instruction_ref is not None
        if self.instruction_ref.startswith("sha256:"):
            return self.instruction_ref.split(":", 1)[1]
        return hashlib.sha256(self.instruction_ref.encode("utf-8")).hexdigest()

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "repository": self.repository,
            "base_commit": self.base_commit,
            "instructions": self.instructions,
            "instruction_ref": self.instruction_ref,
            "instruction_digest": self.instruction_digest,
            "agent_id": self.agent_id,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "coding_harness": self.coding_harness,
            "permitted_runtimes": list(self.permitted_runtimes),
            "authorized_operations": list(self.authorized_operations),
            "file_scope": self.file_scope.to_dict(),
            "network": self.network.to_dict(),
            "isolation": self.isolation.to_dict(),
            "resource_limits": self.resource_limits.to_dict(),
            "approval_class": self.approval_class,
            "consequence_class": self.consequence_class,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.canonical_payload())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def proposal_digest(self) -> str:
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        return {**self.canonical_payload(), "proposal_digest": self.digest}


@dataclass(frozen=True)
class ExecutionGrant:
    """A contract-only grant binding one proposal to one attempt.

    This record is intentionally unsigned.  Possession of an ``ExecutionGrant``
    is not proof of authority, and structural validation cannot enforce
    authenticity, replay prevention or single use.  Those properties require
    the independent execution authority described by ADR-006.
    """

    grant_id: str
    proposal_digest: str
    attempt_id: str
    task_id: str
    request_id: str
    adapter_id: str
    runtime: str
    repository: str
    base_commit: str
    agent_id: str
    model_provider: str
    model_id: str
    coding_harness: str
    file_scope: FileScope
    authorized_operations: tuple[str, ...]
    nonce: str
    issued_at: str
    expires_at: str
    issuer: str
    single_use: bool = True
    integrity: str = UNSIGNED_GRANT_INTEGRITY
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported execution grant schema version")
        _identifier(self.grant_id, "grant_id")
        _sha256(self.proposal_digest, "proposal_digest")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.task_id, "task_id")
        _identifier(self.request_id, "request_id")
        _identifier(self.adapter_id, "adapter_id")
        if self.runtime not in EXECUTION_RUNTIMES:
            raise ContractValidationError("unknown grant runtime")
        _repository(self.repository)
        _commit(self.base_commit)
        _agent_id(self.agent_id)
        _provider_id(self.model_provider)
        _model_id(self.model_id)
        _harness_id(self.coding_harness)
        if not isinstance(self.file_scope, FileScope):
            raise ContractValidationError("file_scope must be FileScope")
        object.__setattr__(
            self, "authorized_operations",
            _string_tuple(
                self.authorized_operations, "authorized_operations",
                allowed=AUTHORIZED_OPERATIONS,
            ),
        )
        if not isinstance(self.nonce, str) or not _NONCE.fullmatch(self.nonce):
            raise ContractValidationError(
                "nonce must be 32-128 safe random characters"
            )
        issued_at = _timestamp(self.issued_at, "issued_at")
        expires_at = _timestamp(self.expires_at, "expires_at")
        if _parse_timestamp(expires_at) <= _parse_timestamp(issued_at):
            raise ContractValidationError("expires_at must be after issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        _identifier(self.issuer, "issuer")
        if self.single_use is not True:
            raise ContractValidationError("execution grants must be single-use")
        if self.integrity != UNSIGNED_GRANT_INTEGRITY:
            raise ContractValidationError(
                "this schema accepts only unsigned contract-only grants"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "proposal_digest": self.proposal_digest,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "adapter_id": self.adapter_id,
            "runtime": self.runtime,
            "repository": self.repository,
            "base_commit": self.base_commit,
            "agent_id": self.agent_id,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "coding_harness": self.coding_harness,
            "file_scope": self.file_scope.to_dict(),
            "authorized_operations": list(self.authorized_operations),
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "issuer": self.issuer,
            "single_use": self.single_use,
            "integrity": self.integrity,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def is_expired(self, now: datetime | str | None = None) -> bool:
        return _coerce_now(now) >= _parse_timestamp(self.expires_at)

    def validate_expiry(self, now: datetime | str | None = None) -> None:
        current = _coerce_now(now)
        if current < _parse_timestamp(self.issued_at):
            raise GrantValidationError("grant_not_yet_valid")
        if current >= _parse_timestamp(self.expires_at):
            raise GrantValidationError("grant_expired")

    def validate_binding(
        self,
        task: TaskSpec,
        *,
        attempt_id: str,
        adapter_id: str,
        runtime: str,
        now: datetime | str | None = None,
        consumed: bool = False,
    ) -> None:
        """Validate this grant against one proposal and audience.

        This is contract validation only.  It cannot prove that an authority
        issued the grant or that a caller has not copied it.
        """

        if not isinstance(task, TaskSpec):
            raise GrantValidationError("task_mismatch")
        if not isinstance(consumed, bool):
            raise GrantValidationError("consumed must be a boolean")
        if consumed:
            raise GrantValidationError("grant_already_consumed")
        if self.single_use is not True:
            raise GrantValidationError("grant_not_single_use")
        self.validate_expiry(now)
        if self.proposal_digest != task.digest:
            raise GrantValidationError("proposal_digest_mismatch")
        if self.attempt_id != attempt_id:
            raise GrantValidationError("attempt_id_mismatch")
        if self.adapter_id != adapter_id:
            raise GrantValidationError("adapter_audience_mismatch")
        if self.runtime != runtime:
            raise GrantValidationError("runtime_audience_mismatch")
        if runtime not in task.permitted_runtimes:
            raise GrantValidationError("runtime_not_permitted")
        if self.task_id != task.task_id or self.request_id != task.request_id:
            raise GrantValidationError("task_identity_mismatch")
        if self.repository != task.repository or self.base_commit != task.base_commit:
            raise GrantValidationError("repository_or_base_mismatch")
        if (
            self.agent_id != task.agent_id
            or self.model_provider != task.model_provider
            or self.model_id != task.model_id
            or self.coding_harness != task.coding_harness
        ):
            raise GrantValidationError("agent_identity_mismatch")
        if self.file_scope != task.file_scope:
            raise GrantValidationError("file_scope_mismatch")
        if self.authorized_operations != task.authorized_operations:
            raise GrantValidationError("authorized_operations_mismatch")


@dataclass(frozen=True)
class ArtifactReference:
    uri: str
    sha256: str
    media_type: str | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        _uri(self.uri, "artifact uri")
        _sha256(self.sha256, "artifact sha256")
        if self.media_type is not None and (
            not isinstance(self.media_type, str)
            or not _MEDIA_TYPE.fullmatch(self.media_type)
        ):
            raise ContractValidationError("artifact media_type is invalid")
        if self.size_bytes is not None:
            _bounded_integer(
                self.size_bytes, "artifact size_bytes",
                minimum=0, maximum=10_485_760,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class TestEvidence:
    name: str
    status: str
    evidence_ref: str | None = None
    command: str | None = None

    def __post_init__(self) -> None:
        _text(self.name, "test evidence name", maximum=300)
        if self.status not in TEST_EVIDENCE_STATUSES:
            raise ContractValidationError("unknown test evidence status")
        if self.evidence_ref is not None:
            _uri(self.evidence_ref, "test evidence_ref")
        object.__setattr__(
            self, "command", _optional_text(self.command, "test command", maximum=2_000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "evidence_ref": self.evidence_ref,
            "command": self.command,
        }


@dataclass(frozen=True)
class ExecutionError:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _REASON_CODE.fullmatch(self.code):
            raise ContractValidationError("execution error code is invalid")
        _text(self.message, "execution error message", maximum=2_000)
        if not isinstance(self.retryable, bool):
            raise ContractValidationError("execution error retryable must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class ResourceUsage:
    wall_time_seconds: float | None = None
    cpu_seconds: float | None = None
    peak_memory_mb: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        for name in ("wall_time_seconds", "cpu_seconds", "cost_usd"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _non_negative_number(value, name))
        for name in ("peak_memory_mb", "input_tokens", "output_tokens"):
            value = getattr(self, name)
            if value is not None:
                _bounded_integer(
                    value, name, minimum=0, maximum=1_000_000_000,
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_time_seconds": self.wall_time_seconds,
            "cpu_seconds": self.cpu_seconds,
            "peak_memory_mb": self.peak_memory_mb,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class ExecutionReport:
    """An untrusted report produced by a worker or adapter.

    The ``trust`` field is fixed.  A report can describe success, but it cannot
    assert verified completion; see :meth:`is_verified_by`.
    """

    report_id: str
    task_id: str
    proposal_digest: str
    attempt_id: str
    provider_run_id: str
    adapter_id: str
    runtime: str
    agent_id: str
    model_provider: str
    model_id: str
    coding_harness: str
    outcome: str
    artifacts: tuple[ArtifactReference, ...] = ()
    commit_sha: str | None = None
    pull_request: str | None = None
    test_evidence: tuple[TestEvidence, ...] = ()
    errors: tuple[ExecutionError, ...] = ()
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    started_at: str | None = None
    finished_at: str | None = None
    trust: str = UNTRUSTED_REPORT_TRUST
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError("unsupported execution report schema version")
        _identifier(self.report_id, "report_id")
        _identifier(self.task_id, "task_id")
        _sha256(self.proposal_digest, "proposal_digest")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.provider_run_id, "provider_run_id")
        _identifier(self.adapter_id, "adapter_id")
        if self.runtime not in EXECUTION_RUNTIMES:
            raise ContractValidationError("unknown report runtime")
        _agent_id(self.agent_id)
        _provider_id(self.model_provider)
        _model_id(self.model_id)
        _harness_id(self.coding_harness)
        if self.outcome not in EXECUTION_OUTCOMES:
            raise ContractValidationError("unknown execution outcome")
        if not isinstance(self.artifacts, (tuple, list)) or not all(
            isinstance(item, ArtifactReference) for item in self.artifacts
        ):
            raise ContractValidationError(
                "artifacts must contain ArtifactReference values"
            )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if self.commit_sha is not None:
            _commit(self.commit_sha, "commit_sha")
        if self.pull_request is not None and (
            not isinstance(self.pull_request, str)
            or not _PULL_REQUEST.fullmatch(self.pull_request)
        ):
            raise ContractValidationError(
                "pull_request must be an exact owner/repository#number reference"
            )
        if not isinstance(self.test_evidence, (tuple, list)) or not all(
            isinstance(item, TestEvidence) for item in self.test_evidence
        ):
            raise ContractValidationError(
                "test_evidence must contain TestEvidence values"
            )
        object.__setattr__(self, "test_evidence", tuple(self.test_evidence))
        if not isinstance(self.errors, (tuple, list)) or not all(
            isinstance(item, ExecutionError) for item in self.errors
        ):
            raise ContractValidationError("errors must contain ExecutionError values")
        object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.resource_usage, ResourceUsage):
            raise ContractValidationError("resource_usage must be ResourceUsage")
        started_at = _optional_timestamp(self.started_at, "started_at")
        finished_at = _optional_timestamp(self.finished_at, "finished_at")
        if started_at is not None and finished_at is not None:
            if _parse_timestamp(finished_at) < _parse_timestamp(started_at):
                raise ContractValidationError(
                    "finished_at must not precede started_at"
                )
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)
        if self.trust != UNTRUSTED_REPORT_TRUST:
            raise ContractValidationError(
                "worker reports are untrusted and cannot declare another trust level"
            )

    @property
    def completion_verified(self) -> bool:
        """A worker report never establishes verified completion."""

        return False

    def is_verified_by(self, verification: "VerificationRecord") -> bool:
        return verification.verifies(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "task_id": self.task_id,
            "proposal_digest": self.proposal_digest,
            "attempt_id": self.attempt_id,
            "provider_run_id": self.provider_run_id,
            "adapter_id": self.adapter_id,
            "runtime": self.runtime,
            "agent_id": self.agent_id,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "coding_harness": self.coding_harness,
            "outcome": self.outcome,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "commit_sha": self.commit_sha,
            "pull_request": self.pull_request,
            "test_evidence": [
                evidence.to_dict() for evidence in self.test_evidence
            ],
            "errors": [error.to_dict() for error in self.errors],
            "resource_usage": self.resource_usage.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trust": self.trust,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class EvidenceReference:
    kind: str
    uri: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _REASON_CODE.fullmatch(self.kind):
            raise ContractValidationError("evidence kind is invalid")
        _uri(self.uri, "evidence uri")
        if self.sha256 is not None:
            _sha256(self.sha256, "evidence sha256")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "uri": self.uri, "sha256": self.sha256}


@dataclass(frozen=True)
class ValidationOutcome:
    claim: str
    result: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _text(self.claim, "validation claim", maximum=1_000)
        if self.result not in VERIFICATION_RESULTS:
            raise ContractValidationError("unknown validation result")
        object.__setattr__(
            self, "detail", _optional_text(self.detail, "validation detail", maximum=2_000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "result": self.result,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class VerificationRecord:
    """An independent verification result, kept separate from worker reports."""

    verification_id: str
    task_id: str
    proposal_digest: str
    attempt_id: str
    verifier_id: str
    method: str
    evidence_checked: tuple[EvidenceReference, ...]
    outcomes: tuple[ValidationOutcome, ...]
    status: str
    verified_at: str
    failure_reasons: tuple[str, ...] = ()
    scope: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError(
                "unsupported verification record schema version"
            )
        _identifier(self.verification_id, "verification_id")
        _identifier(self.task_id, "task_id")
        _sha256(self.proposal_digest, "proposal_digest")
        _identifier(self.attempt_id, "attempt_id")
        _identifier(self.verifier_id, "verifier_id")
        _identifier(self.method, "method")
        if not isinstance(self.evidence_checked, (tuple, list)) or not all(
            isinstance(item, EvidenceReference) for item in self.evidence_checked
        ):
            raise ContractValidationError(
                "evidence_checked must contain EvidenceReference values"
            )
        object.__setattr__(self, "evidence_checked", tuple(self.evidence_checked))
        if not isinstance(self.outcomes, (tuple, list)) or not all(
            isinstance(item, ValidationOutcome) for item in self.outcomes
        ):
            raise ContractValidationError(
                "outcomes must contain ValidationOutcome values"
            )
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        if self.status not in VERIFICATION_RESULTS:
            raise ContractValidationError("unknown verification status")
        object.__setattr__(
            self, "verified_at", _timestamp(self.verified_at, "verified_at"),
        )
        if not isinstance(self.failure_reasons, (tuple, list)):
            raise ContractValidationError("failure_reasons must be a tuple or list")
        reasons = tuple(
            _text(reason, "failure reason", maximum=1_000)
            for reason in self.failure_reasons
        )
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(
            self, "scope", _optional_text(self.scope, "scope", maximum=2_000),
        )
        if self.status == "pass":
            if not self.evidence_checked:
                raise ContractValidationError(
                    "a passing verification requires checked evidence"
                )
            if not self.outcomes:
                raise ContractValidationError(
                    "a passing verification requires at least one outcome"
                )
            if reasons:
                raise ContractValidationError(
                    "a passing verification must not contain failure reasons"
                )
            if any(outcome.result != "pass" for outcome in self.outcomes):
                raise ContractValidationError(
                    "a passing verification requires every outcome to pass"
                )

    @property
    def verified_completion(self) -> bool:
        return self.status == "pass" and not self.failure_reasons

    def verifies(self, report: ExecutionReport) -> bool:
        if not isinstance(report, ExecutionReport):
            return False
        return (
            self.verified_completion
            and self.task_id == report.task_id
            and self.proposal_digest == report.proposal_digest
            and self.attempt_id == report.attempt_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verification_id": self.verification_id,
            "task_id": self.task_id,
            "proposal_digest": self.proposal_digest,
            "attempt_id": self.attempt_id,
            "verifier_id": self.verifier_id,
            "method": self.method,
            "evidence_checked": [
                evidence.to_dict() for evidence in self.evidence_checked
            ],
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "status": self.status,
            "verified_at": self.verified_at,
            "failure_reasons": list(self.failure_reasons),
            "scope": self.scope,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())
