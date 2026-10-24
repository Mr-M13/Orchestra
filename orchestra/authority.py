"""Disposable independent execution-authority prototype.

This module is deliberately a **prototype** and is not connected to OMCP,
systemd, MCP Tunnel, Nextcloud, Docker, PixelBox or any live dispatch path.
It implements the durable, fail-closed parts of ADR-006 that can be exercised
with a fake worker in a temporary directory:

* immutable proposal storage bound to the canonical :class:`TaskSpec` digest;
* simulated human approval and bounded standing authorisation;
* single-use, expiring, audience-bound execution grants;
* an append-preserving audit log;
* untrusted execution reports and separate independent verification;
* cancellation, stale-attempt detection and unknown-outcome reconciliation.

The class name includes ``Prototype`` on purpose.  It is a library-level
prototype, not a service, and it does not authenticate principals, sign grants,
enforce operating-system identity or isolate credentials.  Those properties
require the separate-process boundary documented in
``docs/decisions/ADR-007-execution-authority-prototype.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from orchestra.contracts import (
    AUTHORIZED_OPERATIONS,
    EXECUTION_RUNTIMES,
    ArtifactReference,
    ContractValidationError,
    EvidenceReference,
    ExecutionError,
    ExecutionGrant,
    ExecutionReport,
    FileScope,
    GrantValidationError,
    IsolationRequirements,
    NetworkPolicy,
    ResourceLimits,
    ResourceUsage,
    TaskSpec,
    TestEvidence,
    ValidationOutcome,
    VerificationRecord,
    canonical_json,
)


PROTOTYPE_MODE = "disposable_prototype"
SCHEMA_VERSION = 1

#: Operations that may appear in a bounded routine standing authorisation.
STANDING_OPERATIONS = frozenset({
    "read_repository",
    "edit_workspace",
    "run_commands",
    "run_tests",
    "write_artifacts",
    "create_branch",
})
#: Operations that must never be covered by standing authorisation.
SENSITIVE_OPERATIONS = frozenset({
    "push_branch",
    "open_pull_request",
    "merge",
    "deploy",
    "access_network",
    "use_credentials",
})

PRINCIPAL_KINDS = frozenset({
    "coordinator", "human", "verifier", "operator", "worker",
})

ATTEMPT_GRANTED = "granted"
ATTEMPT_DISPATCHING = "dispatching"
ATTEMPT_DISPATCHED = "dispatched"
ATTEMPT_REPORTED = "reported"
ATTEMPT_VERIFIED = "verified"
ATTEMPT_REJECTED = "rejected"
ATTEMPT_EXPIRED = "expired"
ATTEMPT_CANCELLED = "cancelled"
ATTEMPT_CANCELLED_UNCERTAIN = "cancelled_uncertain"
ATTEMPT_UNKNOWN = "unknown"
ATTEMPT_FAILED_NO_SIDE_EFFECT = "failed_no_side_effect"

#: States that mean a new attempt for the same request could duplicate an
#: uncertain or completed external side effect.  Only safe, provably
#: non-executed states permit a new attempt.
UNSAFE_RETRY_STATES = frozenset({
    ATTEMPT_GRANTED,
    ATTEMPT_DISPATCHING,
    ATTEMPT_DISPATCHED,
    ATTEMPT_REPORTED,
    ATTEMPT_VERIFIED,
    ATTEMPT_REJECTED,
    ATTEMPT_CANCELLED_UNCERTAIN,
    ATTEMPT_UNKNOWN,
})
TERMINAL_STATES = frozenset({
    ATTEMPT_VERIFIED,
    ATTEMPT_REJECTED,
    ATTEMPT_EXPIRED,
    ATTEMPT_CANCELLED,
    ATTEMPT_FAILED_NO_SIDE_EFFECT,
})
RECONCILABLE_STATES = frozenset({
    ATTEMPT_UNKNOWN,
    ATTEMPT_CANCELLED_UNCERTAIN,
})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_PATH_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthorityError(Exception):
    """Base class for authority refusals."""


class AuthorityConfigurationError(AuthorityError):
    """The policy or durable database configuration is not acceptable."""


class UnauthorizedPrincipalError(AuthorityError):
    """A principal tried to perform an operation outside its role."""


class UnauthorizedApprovalError(UnauthorizedPrincipalError):
    """Only a trusted simulated human principal may approve a task."""


class UnauthorizedVerifierError(UnauthorizedPrincipalError):
    """Only a trusted independent verifier may submit verification."""


class UnauthorizedReconciliationError(UnauthorizedPrincipalError):
    """Only a trusted operator may reconcile an unknown outcome."""


class ProposalConflictError(AuthorityError):
    """A request id was reused for a different canonical proposal."""


class ProposalNotFoundError(AuthorityError):
    """The proposal was never registered with this authority."""


class ProposalMismatchError(AuthorityError):
    """A caller presented a proposal that does not match durable state."""


class ApprovalRequiredError(AuthorityError):
    """No current human approval or bounded standing authorisation applies."""


class ApprovalDeniedError(AuthorityError):
    """A current human decision explicitly denies the proposal."""


class ApprovalConflictError(AuthorityError):
    """Contradictory approval decisions were attempted."""


class AudienceNotAllowedError(AuthorityError):
    """The agent/model/harness/adapter/runtime audience is not configured."""


class GrantNotIssuedError(AuthorityError):
    """The grant is not present in the authority's durable state."""


class GrantTamperedError(AuthorityError):
    """The grant presented differs from the grant that was issued."""


class GrantReplayError(AuthorityError):
    """A single-use grant was already consumed."""


class GrantExpiredError(AuthorityError):
    """A grant or approval is outside its validity window."""


class GrantRevokedError(AuthorityError):
    """A grant was revoked before consumption."""


class IdempotencyConflictError(AuthorityError):
    """An idempotency key was reused with different request parameters."""


class UnsafeRetryError(AuthorityError):
    """A new attempt would duplicate an uncertain external side effect."""


class AttemptStateError(AuthorityError):
    """The attempt is not in a state that permits the requested transition."""


class ReconciliationRequiredError(AuthorityError):
    """An unknown outcome must be reconciled before a report is accepted."""


class ReportConflictError(AuthorityError):
    """A second, different worker report was submitted for one attempt."""


class VerificationConflictError(AuthorityError):
    """Contradictory verification evidence was submitted."""


class VerificationBindingError(AuthorityError):
    """A verification record does not bind to the stored report or attempt."""


# ---------------------------------------------------------------------------
# Policy and principals
# ---------------------------------------------------------------------------


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise AuthorityConfigurationError(f"{field} is not a safe identifier")
    return value


def _timestamp(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise AuthorityConfigurationError(
                f"{field} must be an ISO-8601 timestamp"
            ) from None
    else:
        raise AuthorityConfigurationError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorityConfigurationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, field)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _path_within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _normalise_root(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise AuthorityConfigurationError(f"{field} contains an invalid path")
    if value.startswith("/") or value.startswith("~"):
        raise AuthorityConfigurationError(f"{field} must be repository-relative")
    normalised = value.rstrip("/")
    if not normalised or normalised == ".":
        raise AuthorityConfigurationError(f"{field} cannot be the workspace root")
    if any(not _PATH_PART.fullmatch(part) or part in {".", ".."}
           for part in normalised.split("/")):
        raise AuthorityConfigurationError(f"{field} contains an unsafe path")
    return normalised


@dataclass(frozen=True)
class Principal:
    """A simulated principal.

    The role is asserted in-process; this prototype does not authenticate it.
    The kind/identity split is what lets tests demonstrate that a coordinator
    cannot use an approval method even if it knows an owner's identifier.
    """

    principal_id: str
    kind: str

    def __post_init__(self) -> None:
        _safe_id(self.principal_id, "principal_id")
        if self.kind not in PRINCIPAL_KINDS:
            raise AuthorityConfigurationError("unknown principal kind")

    def to_dict(self) -> dict[str, str]:
        return {"principal_id": self.principal_id, "kind": self.kind}


@dataclass(frozen=True)
class ExecutionAudience:
    """The exact execution audience a grant may name."""

    adapter_id: str
    runtime: str
    agent_id: str
    model_provider: str
    model_id: str
    coding_harness: str

    def __post_init__(self) -> None:
        for field in ("adapter_id", "agent_id", "model_provider", "model_id",
                      "coding_harness"):
            _safe_id(getattr(self, field), field)
        if self.runtime not in EXECUTION_RUNTIMES:
            raise AuthorityConfigurationError("unknown execution runtime")

    def key(self) -> tuple[str, ...]:
        return (
            self.adapter_id,
            self.runtime,
            self.agent_id,
            self.model_provider,
            self.model_id,
            self.coding_harness,
        )

    def matches(self, task: TaskSpec, adapter_id: str, runtime: str) -> bool:
        return (
            self.adapter_id == adapter_id
            and self.runtime == runtime
            and self.agent_id == task.agent_id
            and self.model_provider == task.model_provider
            and self.model_id == task.model_id
            and self.coding_harness == task.coding_harness
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "adapter_id": self.adapter_id,
            "runtime": self.runtime,
            "agent_id": self.agent_id,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "coding_harness": self.coding_harness,
        }


@dataclass(frozen=True)
class StandingAuthorization:
    """An owner-configured, exact, bounded authorisation for routine work.

    A rule can never cover a sensitive operation or consequence class, network
    access, credential use, deployment, merge, push or pull-request creation.
    """

    policy_id: str
    repository: str
    agent_id: str
    model_provider: str
    model_id: str
    coding_harness: str
    adapter_id: str
    runtime: str
    allowed_operations: tuple[str, ...]
    write_roots: tuple[str, ...] = ()
    max_runtime_seconds: int = 1_800
    valid_from: str | None = None
    valid_until: str | None = None

    def __post_init__(self) -> None:
        for field in ("policy_id", "agent_id", "model_provider", "model_id",
                      "coding_harness", "adapter_id"):
            _safe_id(getattr(self, field), field)
        if not re.fullmatch(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", self.repository):
            raise AuthorityConfigurationError("repository must be owner/name")
        if self.runtime not in EXECUTION_RUNTIMES:
            raise AuthorityConfigurationError("unknown standing runtime")

        operations = tuple(sorted(set(self.allowed_operations)))
        if not operations:
            raise AuthorityConfigurationError(
                "standing authorisation requires at least one operation"
            )
        unknown = set(operations) - AUTHORIZED_OPERATIONS
        if unknown:
            raise AuthorityConfigurationError(
                f"standing authorisation contains unknown operations: {sorted(unknown)}"
            )
        forbidden = set(operations) & SENSITIVE_OPERATIONS
        if forbidden or set(operations) - STANDING_OPERATIONS:
            raise AuthorityConfigurationError(
                "standing authorisation may only cover bounded routine operations"
            )
        object.__setattr__(self, "allowed_operations", operations)

        roots = tuple(sorted({_normalise_root(root, "write_roots")
                              for root in self.write_roots}))
        object.__setattr__(self, "write_roots", roots)

        if not isinstance(self.max_runtime_seconds, int) or isinstance(
            self.max_runtime_seconds, bool
        ) or not 1 <= self.max_runtime_seconds <= 604_800:
            raise AuthorityConfigurationError(
                "max_runtime_seconds must be between 1 and 604800"
            )
        object.__setattr__(
            self, "valid_from", _optional_timestamp(self.valid_from, "valid_from"),
        )
        object.__setattr__(
            self, "valid_until", _optional_timestamp(self.valid_until, "valid_until"),
        )
        if self.valid_from is not None and self.valid_until is not None:
            if _parse_timestamp(self.valid_until) <= _parse_timestamp(self.valid_from):
                raise AuthorityConfigurationError(
                    "valid_until must be after valid_from"
                )

    def in_window(self, now: datetime) -> bool:
        if self.valid_from is not None and now < _parse_timestamp(self.valid_from):
            return False
        if self.valid_until is not None and now >= _parse_timestamp(self.valid_until):
            return False
        return True

    def matches(self, task: TaskSpec, adapter_id: str, runtime: str,
                now: datetime) -> bool:
        if not self.in_window(now):
            return False
        if task.approval_class != "routine":
            return False
        if task.consequence_class != "routine_development":
            return False
        if self.repository != task.repository:
            return False
        if (
            self.agent_id != task.agent_id
            or self.model_provider != task.model_provider
            or self.model_id != task.model_id
            or self.coding_harness != task.coding_harness
        ):
            return False
        if self.adapter_id != adapter_id or self.runtime != runtime:
            return False
        if runtime not in task.permitted_runtimes:
            return False
        if task.authorized_operations != self.allowed_operations:
            return False
        if set(task.authorized_operations) & SENSITIVE_OPERATIONS:
            return False
        if task.network.mode != "denied":
            return False
        if task.isolation.filesystem not in {"workspace_only", "ephemeral_workspace"}:
            return False
        if task.isolation.credentials not in {"broker_only", "runner_scoped"}:
            return False
        if task.isolation.git_metadata != "read_only":
            return False
        if task.isolation.sandbox not in {
            "required", "provider_builtin", "ephemeral_runner",
        }:
            return False
        if task.resource_limits.max_runtime_seconds > self.max_runtime_seconds:
            return False
        return all(_path_within(path, self.write_roots)
                   for path in task.file_scope.write_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "repository": self.repository,
            "agent_id": self.agent_id,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "coding_harness": self.coding_harness,
            "adapter_id": self.adapter_id,
            "runtime": self.runtime,
            "allowed_operations": list(self.allowed_operations),
            "write_roots": list(self.write_roots),
            "max_runtime_seconds": self.max_runtime_seconds,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
        }


@dataclass(frozen=True)
class AuthorityPolicy:
    """Immutable owner policy.

    There is no authority API that mutates this object.  A database is bound to
    one policy digest, so reopening it with a widened policy is rejected.
    """

    authority_id: str
    trusted_humans: frozenset[str]
    trusted_verifiers: frozenset[str]
    trusted_operators: frozenset[str]
    audiences: tuple[ExecutionAudience, ...]
    standing_authorizations: tuple[StandingAuthorization, ...] = ()
    grant_ttl_seconds: int = 300
    approval_ttl_seconds: int = 3_600
    stale_attempt_seconds: int = 3_600

    def __post_init__(self) -> None:
        _safe_id(self.authority_id, "authority_id")
        for field in ("trusted_humans", "trusted_verifiers", "trusted_operators"):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list, set, frozenset)) or not values:
                raise AuthorityConfigurationError(f"{field} must not be empty")
            checked = frozenset(_safe_id(value, field) for value in values)
            object.__setattr__(self, field, checked)

        audiences = tuple(sorted(self.audiences, key=lambda item: item.key()))
        if not audiences:
            raise AuthorityConfigurationError("at least one audience is required")
        keys = [item.key() for item in audiences]
        if len(set(keys)) != len(keys):
            raise AuthorityConfigurationError("duplicate execution audience")
        object.__setattr__(self, "audiences", audiences)

        rules = tuple(sorted(
            self.standing_authorizations, key=lambda item: item.policy_id,
        ))
        ids = [item.policy_id for item in rules]
        if len(set(ids)) != len(ids):
            raise AuthorityConfigurationError(
                "duplicate standing authorisation policy id"
            )
        object.__setattr__(self, "standing_authorizations", rules)

        for field, maximum in (
            ("grant_ttl_seconds", 3_600),
            ("approval_ttl_seconds", 604_800),
            ("stale_attempt_seconds", 604_800),
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or not (
                1 <= value <= maximum
            ):
                raise AuthorityConfigurationError(
                    f"{field} must be between 1 and {maximum}"
                )

    def find_audience(self, task: TaskSpec, adapter_id: str,
                      runtime: str) -> ExecutionAudience | None:
        return next(
            (audience for audience in self.audiences
             if audience.matches(task, adapter_id, runtime)),
            None,
        )

    def matching_standing(self, task: TaskSpec, adapter_id: str,
                          runtime: str, now: datetime) -> StandingAuthorization | None:
        return next(
            (rule for rule in self.standing_authorizations
             if rule.matches(task, adapter_id, runtime, now)),
            None,
        )

    def require_human(self, principal: Principal) -> None:
        if principal.kind != "human":
            raise UnauthorizedApprovalError(
                "only a simulated human principal may decide approval"
            )
        if principal.principal_id not in self.trusted_humans:
            raise UnauthorizedApprovalError("principal is not a trusted human")

    def require_verifier(self, principal: Principal) -> None:
        if principal.kind != "verifier":
            raise UnauthorizedVerifierError(
                "worker reports cannot be promoted to verification"
            )
        if principal.principal_id not in self.trusted_verifiers:
            raise UnauthorizedVerifierError("principal is not a trusted verifier")

    def require_operator(self, principal: Principal) -> None:
        if principal.kind != "operator":
            raise UnauthorizedReconciliationError(
                "only an operator may reconcile an unknown outcome"
            )
        if principal.principal_id not in self.trusted_operators:
            raise UnauthorizedReconciliationError("principal is not a trusted operator")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "trusted_humans": sorted(self.trusted_humans),
            "trusted_verifiers": sorted(self.trusted_verifiers),
            "trusted_operators": sorted(self.trusted_operators),
            "audiences": [audience.to_dict() for audience in self.audiences],
            "standing_authorizations": [
                rule.to_dict() for rule in self.standing_authorizations
            ],
            "grant_ttl_seconds": self.grant_ttl_seconds,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "stale_attempt_seconds": self.stale_attempt_seconds,
        }

    @property
    def policy_digest(self) -> str:
        return _digest(self.canonical_payload())


# ---------------------------------------------------------------------------
# Durable records returned to callers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalRecord:
    proposal_digest: str
    task_id: str
    request_id: str
    task: TaskSpec
    proposer_id: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_digest": self.proposal_digest,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "task": self.task.to_dict(),
            "proposer_id": self.proposer_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    proposal_digest: str
    decision: str
    source: str
    principal_id: str
    policy_id: str | None
    decided_at: str
    expires_at: str | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "proposal_digest": self.proposal_digest,
            "decision": self.decision,
            "source": self.source,
            "principal_id": self.principal_id,
            "policy_id": self.policy_id,
            "decided_at": self.decided_at,
            "expires_at": self.expires_at,
            "reason": self.reason,
        }

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= _parse_timestamp(self.expires_at)


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    idempotency_key: str
    proposal_digest: str
    task_id: str
    request_id: str
    adapter_id: str
    runtime: str
    state: str
    approval_id: str
    grant_id: str | None
    provider_run_id: str | None
    report: ExecutionReport | None
    created_at: str
    updated_at: str
    cancel_requested_at: str | None
    cancellation_reason: str | None
    last_transition_reason: str | None

    @property
    def verified(self) -> bool:
        return self.state == ATTEMPT_VERIFIED

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "proposal_digest": self.proposal_digest,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "adapter_id": self.adapter_id,
            "runtime": self.runtime,
            "state": self.state,
            "approval_id": self.approval_id,
            "grant_id": self.grant_id,
            "provider_run_id": self.provider_run_id,
            "report": self.report.to_dict() if self.report is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancel_requested_at": self.cancel_requested_at,
            "cancellation_reason": self.cancellation_reason,
            "last_transition_reason": self.last_transition_reason,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class IssuedGrant:
    attempt: AttemptRecord
    grant: ExecutionGrant
    approval: ApprovalRecord

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt.to_dict(),
            "grant": self.grant.to_dict(),
            "approval": self.approval.to_dict(),
        }


@dataclass(frozen=True)
class GrantRecord:
    grant_id: str
    attempt_id: str
    proposal_digest: str
    nonce: str
    issued_at: str
    expires_at: str
    consumed_at: str | None
    consumed_by: str | None
    revoked_at: str | None
    revocation_reason: str | None
    grant: ExecutionGrant

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "attempt_id": self.attempt_id,
            "proposal_digest": self.proposal_digest,
            "nonce": self.nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
            "consumed_by": self.consumed_by,
            "revoked_at": self.revoked_at,
            "revocation_reason": self.revocation_reason,
            "grant": self.grant.to_dict(),
        }


@dataclass(frozen=True)
class VerificationDecision:
    record: VerificationRecord
    applied: bool
    final_state: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "applied": self.applied,
            "final_state": self.final_state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CancellationResult:
    cancellation_id: str
    attempt_id: str
    requested_at: str
    applied: bool
    final_state: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cancellation_id": self.cancellation_id,
            "attempt_id": self.attempt_id,
            "requested_at": self.requested_at,
            "applied": self.applied,
            "final_state": self.final_state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RecoveryReport:
    swept_at: str
    expired_grants: tuple[str, ...]
    expired_attempts: tuple[str, ...]
    unknown_attempts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "swept_at": self.swept_at,
            "expired_grants": list(self.expired_grants),
            "expired_attempts": list(self.expired_attempts),
            "unknown_attempts": list(self.unknown_attempts),
        }


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    at: str
    proposal_digest: str | None
    attempt_id: str | None
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "at": self.at,
            "proposal_digest": self.proposal_digest,
            "attempt_id": self.attempt_id,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Schema and conversion helpers
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_digest TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    task_json TEXT NOT NULL,
    proposer_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS proposals_task_request
    ON proposals(task_id, request_id);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_digest TEXT NOT NULL REFERENCES proposals(proposal_digest),
    decision TEXT NOT NULL,
    source TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    policy_id TEXT,
    decided_at TEXT NOT NULL,
    expires_at TEXT,
    reason TEXT,
    approval_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_proposal
    ON approvals(proposal_digest);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    idempotency_fingerprint TEXT NOT NULL,
    proposal_digest TEXT NOT NULL REFERENCES proposals(proposal_digest),
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    state TEXT NOT NULL,
    approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
    grant_id TEXT,
    provider_run_id TEXT,
    report_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancel_requested_at TEXT,
    cancellation_reason TEXT,
    last_transition_reason TEXT
);
CREATE INDEX IF NOT EXISTS attempts_request
    ON attempts(task_id, request_id);
CREATE INDEX IF NOT EXISTS attempts_state
    ON attempts(state);
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    proposal_digest TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    grant_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT,
    revoked_at TEXT,
    revocation_reason TEXT
);
CREATE INDEX IF NOT EXISTS grants_expiry
    ON grants(expires_at);
CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    proposal_digest TEXT NOT NULL,
    verifier_id TEXT NOT NULL,
    status TEXT NOT NULL,
    record_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS verifications_attempt
    ON verifications(attempt_id);
CREATE TABLE IF NOT EXISTS cancellations (
    cancellation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    principal_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    applied_state TEXT NOT NULL,
    applied INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    at TEXT NOT NULL,
    proposal_digest TEXT,
    attempt_id TEXT,
    detail_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
"""


def _task_from_payload(payload: dict[str, Any]) -> TaskSpec:
    data = dict(payload)
    data.pop("proposal_digest", None)
    data["permitted_runtimes"] = tuple(data["permitted_runtimes"])
    data["authorized_operations"] = tuple(data["authorized_operations"])
    data["file_scope"] = FileScope(
        read_paths=tuple(data["file_scope"]["read_paths"]),
        write_paths=tuple(data["file_scope"]["write_paths"]),
    )
    data["network"] = NetworkPolicy(
        mode=data["network"]["mode"],
        allowed_hosts=tuple(data["network"]["allowed_hosts"]),
    )
    data["isolation"] = IsolationRequirements(**data["isolation"])
    data["resource_limits"] = ResourceLimits(**data["resource_limits"])
    data.pop("instruction_digest", None)
    return TaskSpec(**data)


def _grant_from_payload(payload: dict[str, Any]) -> ExecutionGrant:
    data = dict(payload)
    data["file_scope"] = FileScope(
        read_paths=tuple(data["file_scope"]["read_paths"]),
        write_paths=tuple(data["file_scope"]["write_paths"]),
    )
    data["authorized_operations"] = tuple(data["authorized_operations"])
    return ExecutionGrant(**data)


def _report_from_payload(payload: dict[str, Any]) -> ExecutionReport:
    data = dict(payload)
    data["artifacts"] = tuple(
        ArtifactReference(**item) for item in data.get("artifacts", [])
    )
    data["test_evidence"] = tuple(
        TestEvidence(**item) for item in data.get("test_evidence", [])
    )
    data["errors"] = tuple(
        ExecutionError(**item) for item in data.get("errors", [])
    )
    data["resource_usage"] = ResourceUsage(**data.get("resource_usage", {}))
    return ExecutionReport(**data)


def _verification_from_payload(payload: dict[str, Any]) -> VerificationRecord:
    data = dict(payload)
    data["evidence_checked"] = tuple(
        EvidenceReference(**item) for item in data.get("evidence_checked", [])
    )
    data["outcomes"] = tuple(
        ValidationOutcome(**item) for item in data.get("outcomes", [])
    )
    data["failure_reasons"] = tuple(data.get("failure_reasons", []))
    return VerificationRecord(**data)


def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"],
        proposal_digest=row["proposal_digest"],
        decision=row["decision"],
        source=row["source"],
        principal_id=row["principal_id"],
        policy_id=row["policy_id"],
        decided_at=row["decided_at"],
        expires_at=row["expires_at"],
        reason=row["reason"],
    )


def _grant_record_from_row(row: sqlite3.Row) -> GrantRecord:
    grant = _grant_from_payload(json.loads(row["grant_json"]))
    return GrantRecord(
        grant_id=row["grant_id"],
        attempt_id=row["attempt_id"],
        proposal_digest=row["proposal_digest"],
        nonce=row["nonce"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        consumed_by=row["consumed_by"],
        revoked_at=row["revoked_at"],
        revocation_reason=row["revocation_reason"],
        grant=grant,
    )


def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
    report = None
    if row["report_json"] is not None:
        report = _report_from_payload(json.loads(row["report_json"]))
    return AttemptRecord(
        attempt_id=row["attempt_id"],
        idempotency_key=row["idempotency_key"],
        proposal_digest=row["proposal_digest"],
        task_id=row["task_id"],
        request_id=row["request_id"],
        adapter_id=row["adapter_id"],
        runtime=row["runtime"],
        state=row["state"],
        approval_id=row["approval_id"],
        grant_id=row["grant_id"],
        provider_run_id=row["provider_run_id"],
        report=report,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancel_requested_at=row["cancel_requested_at"],
        cancellation_reason=row["cancellation_reason"],
        last_transition_reason=row["last_transition_reason"],
    )


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


class PrototypeExecutionAuthority:
    """A small, SQLite-backed, disposable execution authority.

    Typical lifecycle::

        authority = PrototypeExecutionAuthority(db_path, policy)
        proposal = authority.propose(task, proposer=coordinator)
        authority.decide_approval(proposal.proposal_digest, decision="approved",
                                  approver=human)
        issued = authority.request_execution(task, adapter_id=..., runtime=...,
                                             idempotency_key=..., requester=coordinator)
        authority.consume_grant(task, issued.grant, adapter_id=..., runtime=...,
                                consumer=coordinator)
        authority.record_dispatch(issued.attempt.attempt_id, provider_run_id=...,
                                  adapter_id=..., runtime=..., worker=worker)
        authority.record_report(report, worker=worker)   # still unverified
        authority.record_verification(verification, verifier=verifier)

    Every public mutation is a single SQLite transaction using
    ``BEGIN IMMEDIATE``.  Grant consumption is committed before dispatch, so a
    crash after consumption cannot silently make the grant reusable.
    """

    def __init__(
        self,
        db_path: str | Path,
        policy: AuthorityPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout: float = 5.0,
    ) -> None:
        if not isinstance(policy, AuthorityPolicy):
            raise AuthorityConfigurationError("policy must be AuthorityPolicy")
        if str(db_path) == ":memory:":
            raise AuthorityConfigurationError(
                "the prototype authority requires a durable file-backed database"
            )
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timeout = timeout
        self._initialise()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """No persistent connection is retained; provided for symmetry."""

    def __enter__(self) -> "PrototypeExecutionAuthority":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def authority_id(self) -> str:
        return self._policy.authority_id

    @property
    def policy(self) -> AuthorityPolicy:
        return self._policy

    @property
    def database_path(self) -> Path:
        return self._path

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AuthorityConfigurationError(
                "clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc)

    def _iso_now(self) -> str:
        return _timestamp(self._now(), "now")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._path), timeout=self._timeout, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")
        return connection

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise AuthorityConfigurationError(
                    "unsupported authority database schema version"
                )
            connection.executescript(_SCHEMA)
            if version == 0:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise AuthorityConfigurationError("unsupported authority schema")
            rows = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM meta")
            }
            expected = {
                "prototype_mode": PROTOTYPE_MODE,
                "schema_version": str(SCHEMA_VERSION),
                "authority_id": self._policy.authority_id,
                "policy_digest": self._policy.policy_digest,
            }
            for key, value in expected.items():
                existing = rows.get(key)
                if existing is None:
                    connection.execute(
                        "INSERT INTO meta(key, value) VALUES (?, ?)", (key, value),
                    )
                elif existing != value:
                    raise AuthorityConfigurationError(
                        f"authority database {key} does not match this policy"
                    )
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    # -- audit -------------------------------------------------------------

    @staticmethod
    def _event(connection: sqlite3.Connection, event_type: str, at: str, *,
               proposal_digest: str | None = None,
               attempt_id: str | None = None,
               detail: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO events(event_type, at, proposal_digest, attempt_id, "
            "detail_json) VALUES (?, ?, ?, ?, ?)",
            (event_type, at, proposal_digest, attempt_id,
             canonical_json(detail or {})),
        )

    def events(self) -> tuple[AuditEvent, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY sequence"
            ).fetchall()
        return tuple(
            AuditEvent(
                sequence=row["sequence"],
                event_type=row["event_type"],
                at=row["at"],
                proposal_digest=row["proposal_digest"],
                attempt_id=row["attempt_id"],
                detail=json.loads(row["detail_json"]),
            )
            for row in rows
        )

    # -- proposals and approvals ------------------------------------------

    def propose(
        self,
        task: TaskSpec,
        *,
        proposer: Principal,
    ) -> ProposalRecord:
        if proposer.kind not in {"coordinator", "human"}:
            raise UnauthorizedPrincipalError(
                "only a coordinator or human may register a proposal"
            )
        if not isinstance(task, TaskSpec):
            raise ProposalMismatchError("task must be a TaskSpec")
        now = self._iso_now()
        task_json = task.canonical_json()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM proposals WHERE proposal_digest = ?",
                (task.digest,),
            ).fetchone()
            if existing is not None:
                if existing["task_json"] != task_json:
                    raise ProposalConflictError(
                        "proposal digest matched different canonical bytes"
                    )
                return ProposalRecord(
                    proposal_digest=existing["proposal_digest"],
                    task_id=existing["task_id"],
                    request_id=existing["request_id"],
                    task=_task_from_payload(json.loads(
                        existing["task_json"]
                    )),
                    proposer_id=existing["proposer_id"],
                    created_at=existing["created_at"],
                )
            conflict = connection.execute(
                "SELECT proposal_digest FROM proposals "
                "WHERE task_id = ? AND request_id = ?",
                (task.task_id, task.request_id),
            ).fetchone()
            if conflict is not None:
                raise ProposalConflictError(
                    "task_id/request_id already names a different proposal"
                )
            connection.execute(
                "INSERT INTO proposals(proposal_digest, task_id, request_id, "
                "task_json, proposer_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task.digest, task.task_id, task.request_id, task_json,
                 proposer.principal_id, now),
            )
            self._event(
                connection, "proposal_registered", now,
                proposal_digest=task.digest,
                detail={"task_id": task.task_id, "request_id": task.request_id},
            )
        return ProposalRecord(
            proposal_digest=task.digest,
            task_id=task.task_id,
            request_id=task.request_id,
            task=task,
            proposer_id=proposer.principal_id,
            created_at=now,
        )

    def proposal(self, proposal_digest: str) -> ProposalRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_digest = ?",
                (proposal_digest,),
            ).fetchone()
        if row is None:
            raise ProposalNotFoundError("proposal is not registered")
        return ProposalRecord(
            proposal_digest=row["proposal_digest"],
            task_id=row["task_id"],
            request_id=row["request_id"],
            task=_task_from_payload(json.loads(row["task_json"])),
            proposer_id=row["proposer_id"],
            created_at=row["created_at"],
        )

    def approvals(self, proposal_digest: str) -> tuple[ApprovalRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE proposal_digest = ? "
                "ORDER BY decided_at, approval_id",
                (proposal_digest,),
            ).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    def decide_approval(
        self,
        proposal_digest: str,
        *,
        decision: str,
        approver: Principal,
        approval_id: str | None = None,
        reason: str | None = None,
        expires_at: str | None = None,
    ) -> ApprovalRecord:
        self._policy.require_human(approver)
        if decision not in {"approved", "denied"}:
            raise ApprovalRequiredError("decision must be approved or denied")
        if reason is not None:
            reason = _text_reason(reason)
        now_dt = self._now()
        now = _timestamp(now_dt, "now")
        expiry = (
            _timestamp(expires_at, "expires_at")
            if expires_at is not None
            else _timestamp(
                now_dt + timedelta(seconds=self._policy.approval_ttl_seconds),
                "expires_at",
            )
        )
        if _parse_timestamp(expiry) <= now_dt:
            raise GrantExpiredError("approval expiry must be in the future")
        identifier = approval_id or f"approval-{secrets.token_hex(16)}"
        _safe_id(identifier, "approval_id")

        with self._transaction() as connection:
            proposal = connection.execute(
                "SELECT proposal_digest FROM proposals WHERE proposal_digest = ?",
                (proposal_digest,),
            ).fetchone()
            if proposal is None:
                raise ProposalNotFoundError("proposal is not registered")

            duplicate = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (identifier,),
            ).fetchone()
            if duplicate is not None:
                record = _approval_from_row(duplicate)
                if (
                    record.proposal_digest == proposal_digest
                    and record.decision == decision
                    and record.source == "human"
                    and record.principal_id == approver.principal_id
                ):
                    return record
                raise ApprovalConflictError(
                    "approval_id already names a different decision"
                )

            for row in connection.execute(
                "SELECT * FROM approvals WHERE proposal_digest = ? "
                "AND source = 'human'",
                (proposal_digest,),
            ).fetchall():
                existing = _approval_from_row(row)
                if existing.expired(now_dt):
                    continue
                if existing.decision != decision:
                    raise ApprovalConflictError(
                        "a current contradictory human decision already exists"
                    )

            record = ApprovalRecord(
                approval_id=identifier,
                proposal_digest=proposal_digest,
                decision=decision,
                source="human",
                principal_id=approver.principal_id,
                policy_id=None,
                decided_at=now,
                expires_at=expiry,
                reason=reason,
            )
            connection.execute(
                "INSERT INTO approvals(approval_id, proposal_digest, decision, "
                "source, principal_id, policy_id, decided_at, expires_at, reason, "
                "approval_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.approval_id, record.proposal_digest, record.decision,
                 record.source, record.principal_id, record.policy_id,
                 record.decided_at, record.expires_at, record.reason,
                 canonical_json(record.to_dict())),
            )
            self._event(
                connection, "approval_decided", now,
                proposal_digest=proposal_digest,
                detail={
                    "approval_id": identifier,
                    "decision": decision,
                    "approver": approver.principal_id,
                },
            )
            return record

    def _current_human_approval(
        self, connection: sqlite3.Connection, proposal_digest: str,
        now: datetime,
    ) -> ApprovalRecord:
        rows = connection.execute(
            "SELECT * FROM approvals WHERE proposal_digest = ? "
            "AND source = 'human' ORDER BY decided_at DESC",
            (proposal_digest,),
        ).fetchall()
        approved: ApprovalRecord | None = None
        for row in rows:
            record = _approval_from_row(row)
            if record.expired(now):
                continue
            if record.decision == "denied":
                raise ApprovalDeniedError("a current human decision denies the task")
            if approved is None and record.decision == "approved":
                approved = record
        if approved is None:
            raise ApprovalRequiredError("no current human approval")
        return approved

    def _resolve_approval(
        self,
        connection: sqlite3.Connection,
        task: TaskSpec,
        *,
        adapter_id: str,
        runtime: str,
        now: datetime,
    ) -> ApprovalRecord:
        try:
            return self._current_human_approval(
                connection, task.digest, now,
            )
        except ApprovalDeniedError:
            raise
        except ApprovalRequiredError:
            pass

        rule = self._policy.matching_standing(task, adapter_id, runtime, now)
        if rule is None:
            raise ApprovalRequiredError(
                "no current human approval or exact standing authorisation applies"
            )

        expiry = now + timedelta(seconds=self._policy.approval_ttl_seconds)
        if rule.valid_until is not None:
            expiry = min(expiry, _parse_timestamp(rule.valid_until))
        record = ApprovalRecord(
            approval_id=f"standing-{secrets.token_hex(16)}",
            proposal_digest=task.digest,
            decision="approved",
            source="standing",
            principal_id=f"policy:{rule.policy_id}",
            policy_id=rule.policy_id,
            decided_at=_timestamp(now, "now"),
            expires_at=_timestamp(expiry, "expires_at"),
            reason="bounded routine standing authorisation",
        )
        connection.execute(
            "INSERT INTO approvals(approval_id, proposal_digest, decision, "
            "source, principal_id, policy_id, decided_at, expires_at, reason, "
            "approval_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.approval_id, record.proposal_digest, record.decision,
             record.source, record.principal_id, record.policy_id,
             record.decided_at, record.expires_at, record.reason,
             canonical_json(record.to_dict())),
        )
        self._event(
            connection, "standing_approval_issued", record.decided_at,
            proposal_digest=task.digest,
            detail={
                "approval_id": record.approval_id,
                "policy_id": rule.policy_id,
            },
        )
        return record

    # -- grant issuance ----------------------------------------------------

    def _require_audience(self, task: TaskSpec, adapter_id: str,
                          runtime: str) -> ExecutionAudience:
        if runtime not in task.permitted_runtimes:
            raise AudienceNotAllowedError("runtime is not permitted by the task")
        audience = self._policy.find_audience(task, adapter_id, runtime)
        if audience is None:
            raise AudienceNotAllowedError(
                "exact agent/model/harness/adapter/runtime audience is not configured"
            )
        return audience

    def request_execution(
        self,
        task: TaskSpec,
        *,
        adapter_id: str,
        runtime: str,
        idempotency_key: str,
        requester: Principal,
        grant_ttl_seconds: int | None = None,
    ) -> IssuedGrant:
        if requester.kind not in {"coordinator", "human", "operator"}:
            raise UnauthorizedPrincipalError(
                "only a coordinator, human or operator may request execution"
            )
        _safe_id(adapter_id, "adapter_id")
        _safe_id(idempotency_key, "idempotency_key")
        if runtime not in EXECUTION_RUNTIMES:
            raise AudienceNotAllowedError("unknown runtime")
        ttl = self._policy.grant_ttl_seconds if grant_ttl_seconds is None else (
            grant_ttl_seconds
        )
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not (
            1 <= ttl <= self._policy.grant_ttl_seconds
        ):
            raise AuthorityConfigurationError(
                "grant_ttl_seconds must be within the policy maximum"
            )

        fingerprint = _digest({
            "proposal_digest": task.digest,
            "adapter_id": adapter_id,
            "runtime": runtime,
            "grant_ttl_seconds": ttl,
        })
        now_dt = self._now()
        now = _timestamp(now_dt, "now")

        with self._transaction() as connection:
            proposal_row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_digest = ?",
                (task.digest,),
            ).fetchone()
            if proposal_row is None:
                conflict = connection.execute(
                    "SELECT proposal_digest FROM proposals "
                    "WHERE task_id = ? AND request_id = ?",
                    (task.task_id, task.request_id),
                ).fetchone()
                if conflict is not None:
                    raise ProposalConflictError(
                        "task changed after its proposal was registered"
                    )
                raise ProposalNotFoundError("proposal must be registered first")
            if proposal_row["task_json"] != task.canonical_json():
                raise ProposalMismatchError(
                    "task canonical bytes do not match the stored proposal"
                )

            existing = connection.execute(
                "SELECT * FROM attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["idempotency_fingerprint"] != fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency key was reused with different parameters"
                    )
                return self._issued_grant_for_attempt(connection, existing["attempt_id"])

            unsafe = connection.execute(
                "SELECT attempt_id, state FROM attempts WHERE task_id = ? "
                "AND request_id = ? AND state IN ({})".format(
                    ",".join("?" for _ in UNSAFE_RETRY_STATES)
                ),
                (task.task_id, task.request_id, *sorted(UNSAFE_RETRY_STATES)),
            ).fetchall()
            if unsafe:
                states = sorted({row["state"] for row in unsafe})
                raise UnsafeRetryError(
                    "an existing attempt has not reached a safe retry state: "
                    + ", ".join(states)
                )

            self._require_audience(task, adapter_id, runtime)
            approval = self._resolve_approval(
                connection, task, adapter_id=adapter_id, runtime=runtime,
                now=now_dt,
            )

            attempt_id = f"attempt-{secrets.token_hex(16)}"
            grant_id = f"grant-{secrets.token_hex(16)}"
            expires_at = _timestamp(
                now_dt + timedelta(seconds=ttl), "expires_at",
            )
            nonce = secrets.token_urlsafe(48)
            grant = ExecutionGrant(
                grant_id=grant_id,
                proposal_digest=task.digest,
                attempt_id=attempt_id,
                task_id=task.task_id,
                request_id=task.request_id,
                adapter_id=adapter_id,
                runtime=runtime,
                repository=task.repository,
                base_commit=task.base_commit,
                agent_id=task.agent_id,
                model_provider=task.model_provider,
                model_id=task.model_id,
                coding_harness=task.coding_harness,
                file_scope=task.file_scope,
                authorized_operations=task.authorized_operations,
                nonce=nonce,
                issued_at=now,
                expires_at=expires_at,
                issuer=self._policy.authority_id,
            )

            connection.execute(
                "INSERT INTO attempts(attempt_id, idempotency_key, "
                "idempotency_fingerprint, proposal_digest, task_id, request_id, "
                "adapter_id, runtime, state, approval_id, grant_id, "
                "provider_run_id, report_json, created_at, updated_at, "
                "cancel_requested_at, cancellation_reason, "
                "last_transition_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, "
                "NULL, NULL, NULL)",
                (attempt_id, idempotency_key, fingerprint, task.digest,
                 task.task_id, task.request_id, adapter_id, runtime,
                 ATTEMPT_GRANTED, approval.approval_id, grant_id, now, now),
            )
            connection.execute(
                "INSERT INTO grants(grant_id, attempt_id, proposal_digest, nonce, "
                "grant_json, issued_at, expires_at, consumed_at, consumed_by, "
                "revoked_at, revocation_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                (grant_id, attempt_id, task.digest, nonce,
                 canonical_json(grant.to_dict()), now, expires_at),
            )
            self._event(
                connection, "grant_issued", now,
                proposal_digest=task.digest,
                attempt_id=attempt_id,
                detail={
                    "grant_id": grant_id,
                    "approval_id": approval.approval_id,
                    "approval_source": approval.source,
                    "adapter_id": adapter_id,
                    "runtime": runtime,
                    "expires_at": expires_at,
                },
            )
            return self._issued_grant_for_attempt(connection, attempt_id)

    def _issued_grant_for_attempt(
        self, connection: sqlite3.Connection, attempt_id: str,
    ) -> IssuedGrant:
        attempt_row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,),
        ).fetchone()
        if attempt_row is None:
            raise AttemptStateError("attempt is not registered")
        grant_row = connection.execute(
            "SELECT * FROM grants WHERE attempt_id = ?", (attempt_id,),
        ).fetchone()
        if grant_row is None:
            raise GrantNotIssuedError("attempt has no durable grant")
        approval_row = connection.execute(
            "SELECT * FROM approvals WHERE approval_id = ?",
            (attempt_row["approval_id"],),
        ).fetchone()
        if approval_row is None:
            raise ApprovalRequiredError("attempt has no durable approval")
        return IssuedGrant(
            attempt=_attempt_from_row(attempt_row),
            grant=_grant_record_from_row(grant_row).grant,
            approval=_approval_from_row(approval_row),
        )

    def grant(self, grant_id: str) -> GrantRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM grants WHERE grant_id = ?", (grant_id,),
            ).fetchone()
        if row is None:
            raise GrantNotIssuedError("grant is not registered")
        return _grant_record_from_row(row)

    def _validate_grant_tx(
        self,
        connection: sqlite3.Connection,
        task: TaskSpec,
        grant: ExecutionGrant,
        *,
        adapter_id: str,
        runtime: str,
        now: datetime,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        if not isinstance(task, TaskSpec) or not isinstance(grant, ExecutionGrant):
            raise GrantValidationError("task and grant are required")
        row = connection.execute(
            "SELECT * FROM grants WHERE grant_id = ?", (grant.grant_id,),
        ).fetchone()
        if row is None:
            raise GrantNotIssuedError("grant was not issued by this authority")
        stored = _grant_record_from_row(row)
        if canonical_json(grant.to_dict()) != row["grant_json"]:
            raise GrantTamperedError(
                "grant bytes differ from the durable issued grant"
            )
        if stored.nonce != grant.nonce:
            raise GrantTamperedError("grant nonce does not match durable state")
        if row["revoked_at"] is not None:
            raise GrantRevokedError("grant has been revoked")
        if row["consumed_at"] is not None:
            raise GrantReplayError("single-use grant was already consumed")
        if now >= _parse_timestamp(row["expires_at"]):
            raise GrantExpiredError("grant has expired")
        if now < _parse_timestamp(row["issued_at"]):
            raise GrantValidationError("grant_not_yet_valid")

        attempt_row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (grant.attempt_id,),
        ).fetchone()
        if attempt_row is None:
            raise AttemptStateError("grant names an unknown attempt")
        if attempt_row["state"] != ATTEMPT_GRANTED:
            raise AttemptStateError(
                f"attempt is not awaiting consumption ({attempt_row['state']})"
            )
        if attempt_row["grant_id"] != grant.grant_id:
            raise GrantTamperedError("attempt is bound to a different grant")
        if attempt_row["adapter_id"] != adapter_id:
            raise GrantValidationError("adapter_audience_mismatch")
        if attempt_row["runtime"] != runtime:
            raise GrantValidationError("runtime_audience_mismatch")

        proposal_row = connection.execute(
            "SELECT * FROM proposals WHERE proposal_digest = ?",
            (grant.proposal_digest,),
        ).fetchone()
        if proposal_row is None:
            raise ProposalMismatchError("grant names an unknown proposal")
        if proposal_row["task_json"] != task.canonical_json():
            raise ProposalMismatchError("task does not match the durable proposal")
        if grant.proposal_digest != task.digest:
            raise GrantValidationError("proposal_digest_mismatch")

        self._require_audience(task, adapter_id, runtime)
        grant.validate_binding(
            task,
            attempt_id=grant.attempt_id,
            adapter_id=adapter_id,
            runtime=runtime,
            now=now,
            consumed=False,
        )
        return row, attempt_row, proposal_row

    def validate_grant(
        self,
        task: TaskSpec,
        grant: ExecutionGrant,
        *,
        adapter_id: str,
        runtime: str,
    ) -> GrantRecord:
        now = self._now()
        with self._read() as connection:
            row, _attempt, _proposal = self._validate_grant_tx(
                connection, task, grant, adapter_id=adapter_id, runtime=runtime,
                now=now,
            )
            return _grant_record_from_row(row)

    def _after_consume_hook(
        self, connection: sqlite3.Connection, grant_id: str, attempt_id: str,
    ) -> None:
        """Test seam for crash-before-commit injection; must do nothing."""

    def consume_grant(
        self,
        task: TaskSpec,
        grant: ExecutionGrant,
        *,
        adapter_id: str,
        runtime: str,
        consumer: Principal,
    ) -> GrantRecord:
        if consumer.kind not in {"coordinator", "human", "operator"}:
            raise UnauthorizedPrincipalError(
                "only a coordinator, human or operator may consume a grant"
            )
        now_dt = self._now()
        now = _timestamp(now_dt, "now")
        with self._transaction() as connection:
            row, attempt_row, _proposal = self._validate_grant_tx(
                connection, task, grant, adapter_id=adapter_id, runtime=runtime,
                now=now_dt,
            )
            connection.execute(
                "UPDATE grants SET consumed_at = ?, consumed_by = ? "
                "WHERE grant_id = ? AND consumed_at IS NULL",
                (now, consumer.principal_id, grant.grant_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise GrantReplayError("grant consumption lost an atomic race")
            connection.execute(
                "UPDATE attempts SET state = ?, updated_at = ?, "
                "last_transition_reason = 'grant_consumed' WHERE attempt_id = ?",
                (ATTEMPT_DISPATCHING, now, attempt_row["attempt_id"]),
            )
            self._event(
                connection, "grant_consumed", now,
                proposal_digest=grant.proposal_digest,
                attempt_id=grant.attempt_id,
                detail={
                    "grant_id": grant.grant_id,
                    "consumer": consumer.principal_id,
                },
            )
            self._after_consume_hook(connection, grant.grant_id, grant.attempt_id)
            return _grant_record_from_row(connection.execute(
                "SELECT * FROM grants WHERE grant_id = ?", (grant.grant_id,),
            ).fetchone())

    # -- dispatch and reports ---------------------------------------------

    def attempt(self, attempt_id: str) -> AttemptRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,),
            ).fetchone()
        if row is None:
            raise AttemptStateError("attempt is not registered")
        return _attempt_from_row(row)

    def attempts_for_proposal(self, proposal_digest: str) -> tuple[AttemptRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE proposal_digest = ? "
                "ORDER BY created_at, attempt_id",
                (proposal_digest,),
            ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def record_dispatch(
        self,
        attempt_id: str,
        *,
        provider_run_id: str,
        adapter_id: str,
        runtime: str,
        worker: Principal,
    ) -> AttemptRecord:
        if worker.kind != "worker":
            raise UnauthorizedPrincipalError(
                "only a simulated worker may record a dispatch receipt"
            )
        _safe_id(provider_run_id, "provider_run_id")
        _safe_id(adapter_id, "adapter_id")
        now = self._iso_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT a.*, p.task_json FROM attempts a "
                "JOIN proposals p ON p.proposal_digest = a.proposal_digest "
                "WHERE a.attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise AttemptStateError("attempt is not registered")
            if row["adapter_id"] != adapter_id or row["runtime"] != runtime:
                raise GrantValidationError("dispatch_audience_mismatch")
            if row["provider_run_id"] is not None:
                if row["provider_run_id"] != provider_run_id:
                    raise ReportConflictError(
                        "attempt is already bound to another provider run"
                    )
                return _attempt_from_row(row)
            if row["state"] != ATTEMPT_DISPATCHING:
                raise AttemptStateError(
                    f"dispatch receipt is not acceptable in state {row['state']}"
                )
            connection.execute(
                "UPDATE attempts SET state = ?, provider_run_id = ?, "
                "updated_at = ?, last_transition_reason = 'dispatch_receipt' "
                "WHERE attempt_id = ?",
                (ATTEMPT_DISPATCHED, provider_run_id, now, attempt_id),
            )
            self._event(
                connection, "dispatch_recorded", now,
                proposal_digest=row["proposal_digest"], attempt_id=attempt_id,
                detail={"provider_run_id": provider_run_id},
            )
            return _attempt_from_row(connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,),
            ).fetchone())

    def _validate_report_against_attempt(
        self, report: ExecutionReport, task: TaskSpec, attempt_row: sqlite3.Row,
    ) -> None:
        if not isinstance(report, ExecutionReport):
            raise ReportConflictError("report must be an ExecutionReport")
        if report.task_id != attempt_row["task_id"]:
            raise ReportConflictError("report task_id does not match attempt")
        if report.proposal_digest != attempt_row["proposal_digest"]:
            raise ReportConflictError("report proposal digest does not match")
        if report.attempt_id != attempt_row["attempt_id"]:
            raise ReportConflictError("report attempt_id does not match")
        if report.adapter_id != attempt_row["adapter_id"]:
            raise GrantValidationError("report_adapter_audience_mismatch")
        if report.runtime != attempt_row["runtime"]:
            raise GrantValidationError("report_runtime_audience_mismatch")
        if (
            report.agent_id != task.agent_id
            or report.model_provider != task.model_provider
            or report.model_id != task.model_id
            or report.coding_harness != task.coding_harness
        ):
            raise GrantValidationError("report_agent_identity_mismatch")
        if report.proposal_digest != task.digest:
            raise ReportConflictError("report is not bound to the proposal")
        if (
            attempt_row["provider_run_id"] is not None
            and report.provider_run_id != attempt_row["provider_run_id"]
        ):
            raise ReportConflictError(
                "report provider_run_id does not match the dispatch receipt"
            )

    def record_report(
        self,
        report: ExecutionReport,
        *,
        worker: Principal,
    ) -> AttemptRecord:
        if worker.kind != "worker":
            raise UnauthorizedPrincipalError(
                "only a simulated worker may submit an execution report"
            )
        now = self._iso_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT a.*, p.task_json FROM attempts a "
                "JOIN proposals p ON p.proposal_digest = a.proposal_digest "
                "WHERE a.attempt_id = ?",
                (report.attempt_id,),
            ).fetchone()
            if row is None:
                raise AttemptStateError("report names an unknown attempt")
            task = _task_from_payload(json.loads(row["task_json"]))
            self._validate_report_against_attempt(report, task, row)

            if row["provider_run_id"] is not None and (
                report.provider_run_id != row["provider_run_id"]
            ):
                raise ReportConflictError(
                    "report provider_run_id does not match the dispatch receipt"
                )

            state = row["state"]
            if state == ATTEMPT_VERIFIED:
                if row["report_json"] == canonical_json(report.to_dict()):
                    return _attempt_from_row(row)
                raise ReportConflictError("a different report is already verified")
            if row["report_json"] is not None:
                if row["report_json"] == canonical_json(report.to_dict()):
                    return _attempt_from_row(row)
                raise ReportConflictError(
                    "a different report is already stored for this attempt"
                )

            if state in {ATTEMPT_UNKNOWN}:
                raise ReconciliationRequiredError(
                    "unknown external outcome must be explicitly reconciled"
                )
            if state == ATTEMPT_CANCELLED:
                raise AttemptStateError("report arrived after a safe cancellation")
            if state == ATTEMPT_DISPATCHING and row["provider_run_id"] is None:
                raise AttemptStateError(
                    "a dispatch receipt must be recorded before the report"
                )
            if state not in {
                ATTEMPT_DISPATCHING,
                ATTEMPT_DISPATCHED,
                ATTEMPT_CANCELLED_UNCERTAIN,
            }:
                raise AttemptStateError(
                    f"report is not acceptable in state {state}"
                )

            next_state = (
                ATTEMPT_CANCELLED_UNCERTAIN
                if state == ATTEMPT_CANCELLED_UNCERTAIN
                else ATTEMPT_REPORTED
            )
            connection.execute(
                "UPDATE attempts SET state = ?, report_json = ?, updated_at = ?, "
                "provider_run_id = COALESCE(provider_run_id, ?), "
                "last_transition_reason = ? WHERE attempt_id = ?",
                (next_state, canonical_json(report.to_dict()), now,
                 report.provider_run_id,
                 "report_after_cancellation" if next_state ==
                 ATTEMPT_CANCELLED_UNCERTAIN else "untrusted_report_recorded",
                 report.attempt_id),
            )
            self._event(
                connection, "untrusted_report_recorded", now,
                proposal_digest=row["proposal_digest"],
                attempt_id=report.attempt_id,
                detail={
                    "report_id": report.report_id,
                    "outcome": report.outcome,
                    "trust": report.trust,
                    "state": next_state,
                },
            )
            return _attempt_from_row(connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?",
                (report.attempt_id,),
            ).fetchone())

    def report(self, attempt_id: str) -> ExecutionReport | None:
        return self.attempt(attempt_id).report

    # -- verification ------------------------------------------------------

    def verification_records(self, attempt_id: str) -> tuple[VerificationRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM verifications WHERE attempt_id = ? "
                "ORDER BY recorded_at, verification_id",
                (attempt_id,),
            ).fetchall()
        return tuple(
            _verification_from_payload(json.loads(row["record_json"]))
            for row in rows
        )

    def record_verification(
        self,
        verification: VerificationRecord,
        *,
        verifier: Principal,
    ) -> VerificationDecision:
        self._policy.require_verifier(verifier)
        now = self._iso_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT a.*, p.task_json FROM attempts a "
                "JOIN proposals p ON p.proposal_digest = a.proposal_digest "
                "WHERE a.attempt_id = ?",
                (verification.attempt_id,),
            ).fetchone()
            if row is None:
                raise VerificationBindingError("verification names an unknown attempt")
            if verification.task_id != row["task_id"]:
                raise VerificationBindingError("verification task_id does not match")
            if verification.proposal_digest != row["proposal_digest"]:
                raise VerificationBindingError(
                    "verification proposal digest does not match"
                )
            if verification.attempt_id != row["attempt_id"]:
                raise VerificationBindingError(
                    "verification attempt_id does not match"
                )
            if verification.verifier_id != verifier.principal_id:
                raise VerificationBindingError(
                    "verification verifier_id does not match the trusted principal"
                )

            record_json = canonical_json(verification.to_dict())
            duplicate = connection.execute(
                "SELECT * FROM verifications WHERE verification_id = ?",
                (verification.verification_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["record_json"] != record_json:
                    raise VerificationConflictError(
                        "verification_id already names different evidence"
                    )
                state = row["state"]
                return VerificationDecision(
                    record=verification,
                    applied=state == ATTEMPT_VERIFIED and
                    verification.status == "pass",
                    final_state=state,
                    reason="idempotent_replay",
                )

            existing_rows = connection.execute(
                "SELECT * FROM verifications WHERE attempt_id = ? "
                "ORDER BY recorded_at, verification_id",
                (verification.attempt_id,),
            ).fetchall()
            existing = [
                _verification_from_payload(json.loads(item["record_json"]))
                for item in existing_rows
            ]
            has_pass = any(item.verified_completion for item in existing)
            has_failure = any(item.status != "pass" for item in existing)

            connection.execute(
                "INSERT INTO verifications(verification_id, attempt_id, "
                "proposal_digest, verifier_id, status, record_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (verification.verification_id, verification.attempt_id,
                 verification.proposal_digest, verification.verifier_id,
                 verification.status, record_json, now),
            )

            state = row["state"]
            stored_report = (
                _report_from_payload(json.loads(row["report_json"]))
                if row["report_json"] is not None else None
            )

            if state in RECONCILABLE_STATES or state == ATTEMPT_CANCELLED:
                self._event(
                    connection, "verification_rejected", now,
                    proposal_digest=row["proposal_digest"],
                    attempt_id=verification.attempt_id,
                    detail={
                        "verification_id": verification.verification_id,
                        "reason": f"attempt_state_{state}",
                    },
                )
                return VerificationDecision(
                    record=verification,
                    applied=False,
                    final_state=state,
                    reason=f"attempt_state_{state}",
                )

            if state not in {ATTEMPT_REPORTED, ATTEMPT_VERIFIED, ATTEMPT_REJECTED}:
                self._event(
                    connection, "verification_rejected", now,
                    proposal_digest=row["proposal_digest"],
                    attempt_id=verification.attempt_id,
                    detail={
                        "verification_id": verification.verification_id,
                        "reason": "report_missing",
                    },
                )
                return VerificationDecision(
                    record=verification,
                    applied=False,
                    final_state=state,
                    reason="report_missing",
                )

            if stored_report is None:
                return VerificationDecision(
                    record=verification,
                    applied=False,
                    final_state=state,
                    reason="report_missing",
                )

            if has_pass and verification.status != "pass":
                next_state = ATTEMPT_REJECTED
                connection.execute(
                    "UPDATE attempts SET state = ?, updated_at = ?, "
                    "last_transition_reason = 'verification_contradiction' "
                    "WHERE attempt_id = ?",
                    (next_state, now, verification.attempt_id),
                )
                self._event(
                    connection, "verification_contradiction", now,
                    proposal_digest=row["proposal_digest"],
                    attempt_id=verification.attempt_id,
                    detail={
                        "verification_id": verification.verification_id,
                        "previous": "pass",
                        "new": verification.status,
                        "final_state": next_state,
                    },
                )
                return VerificationDecision(
                    record=verification,
                    applied=True,
                    final_state=next_state,
                    reason="contradictory_evidence_fails_closed",
                )

            if has_failure and verification.status == "pass":
                self._event(
                    connection, "verification_contradiction", now,
                    proposal_digest=row["proposal_digest"],
                    attempt_id=verification.attempt_id,
                    detail={
                        "verification_id": verification.verification_id,
                        "previous": "failure",
                        "new": "pass",
                        "final_state": state,
                    },
                )
                return VerificationDecision(
                    record=verification,
                    applied=False,
                    final_state=state,
                    reason="contradictory_evidence_cannot_upgrade",
                )

            if verification.verified_completion and verification.verifies(stored_report):
                next_state = ATTEMPT_VERIFIED
                reason = None
            else:
                next_state = ATTEMPT_REJECTED
                reason = "independent_verification_failed"

            connection.execute(
                "UPDATE attempts SET state = ?, updated_at = ?, "
                "last_transition_reason = ? WHERE attempt_id = ?",
                (next_state, now, reason or "independent_verification_passed",
                 verification.attempt_id),
            )
            self._event(
                connection, "verification_recorded", now,
                proposal_digest=row["proposal_digest"],
                attempt_id=verification.attempt_id,
                detail={
                    "verification_id": verification.verification_id,
                    "status": verification.status,
                    "final_state": next_state,
                },
            )
            return VerificationDecision(
                record=verification,
                applied=True,
                final_state=next_state,
                reason=reason,
            )

    # -- cancellation, expiry and recovery ---------------------------------

    def cancel_attempt(
        self,
        attempt_id: str,
        *,
        principal: Principal,
        reason: str,
        idempotency_key: str | None = None,
    ) -> CancellationResult:
        if principal.kind not in {"coordinator", "human", "operator"}:
            raise UnauthorizedPrincipalError(
                "only a coordinator, human or operator may cancel an attempt"
            )
        reason = _text_reason(reason)
        key = idempotency_key or f"cancel-{secrets.token_hex(16)}"
        _safe_id(key, "idempotency_key")
        now = self._iso_now()
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT * FROM cancellations WHERE idempotency_key = ?", (key,),
            ).fetchone()
            if duplicate is not None:
                if (
                    duplicate["attempt_id"] != attempt_id
                    or duplicate["principal_id"] != principal.principal_id
                    or duplicate["reason"] != reason
                ):
                    raise IdempotencyConflictError(
                        "cancellation idempotency key was reused differently"
                    )
                return CancellationResult(
                    cancellation_id=duplicate["cancellation_id"],
                    attempt_id=duplicate["attempt_id"],
                    requested_at=duplicate["requested_at"],
                    applied=bool(duplicate["applied"]),
                    final_state=duplicate["applied_state"],
                    reason="idempotent_replay",
                )

            row = connection.execute(
                "SELECT a.*, g.consumed_at AS grant_consumed_at, "
                "g.revoked_at AS grant_revoked_at "
                "FROM attempts a LEFT JOIN grants g ON g.attempt_id = a.attempt_id "
                "WHERE a.attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise AttemptStateError("attempt is not registered")

            state = row["state"]
            if state in TERMINAL_STATES:
                result_state = state
                applied = False
                outcome_reason = "already_terminal"
            elif state == ATTEMPT_UNKNOWN:
                connection.execute(
                    "UPDATE attempts SET cancel_requested_at = COALESCE("
                    "cancel_requested_at, ?), cancellation_reason = ?, "
                    "updated_at = ? WHERE attempt_id = ?",
                    (now, reason, now, attempt_id),
                )
                result_state = ATTEMPT_UNKNOWN
                applied = True
                outcome_reason = "unknown_outcome_requires_reconciliation"
            elif row["grant_consumed_at"] is not None:
                connection.execute(
                    "UPDATE attempts SET state = ?, cancel_requested_at = ?, "
                    "cancellation_reason = ?, updated_at = ?, "
                    "last_transition_reason = 'cancelled_after_consumption' "
                    "WHERE attempt_id = ?",
                    (ATTEMPT_CANCELLED_UNCERTAIN, now, reason, now, attempt_id),
                )
                result_state = ATTEMPT_CANCELLED_UNCERTAIN
                applied = True
                outcome_reason = "external_outcome_uncertain"
            else:
                connection.execute(
                    "UPDATE grants SET revoked_at = COALESCE(revoked_at, ?), "
                    "revocation_reason = COALESCE(revocation_reason, ?) "
                    "WHERE attempt_id = ? AND consumed_at IS NULL",
                    (now, "cancelled_before_consumption", attempt_id),
                )
                connection.execute(
                    "UPDATE attempts SET state = ?, cancel_requested_at = ?, "
                    "cancellation_reason = ?, updated_at = ?, "
                    "last_transition_reason = 'cancelled_before_consumption' "
                    "WHERE attempt_id = ?",
                    (ATTEMPT_CANCELLED, now, reason, now, attempt_id),
                )
                result_state = ATTEMPT_CANCELLED
                applied = True
                outcome_reason = None

            cancellation_id = f"cancellation-{secrets.token_hex(16)}"
            connection.execute(
                "INSERT INTO cancellations(cancellation_id, idempotency_key, "
                "attempt_id, principal_id, reason, requested_at, applied_state, "
                "applied) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cancellation_id, key, attempt_id, principal.principal_id,
                 reason, now, result_state, int(applied)),
            )
            self._event(
                connection, "cancellation_requested", now,
                attempt_id=attempt_id,
                detail={
                    "cancellation_id": cancellation_id,
                    "applied": applied,
                    "final_state": result_state,
                    "reason": outcome_reason,
                },
            )
            return CancellationResult(
                cancellation_id=cancellation_id,
                attempt_id=attempt_id,
                requested_at=now,
                applied=applied,
                final_state=result_state,
                reason=outcome_reason,
            )

    def reconcile_unknown(
        self,
        attempt_id: str,
        *,
        operator: Principal,
        resolution: str,
        reason: str,
        report: ExecutionReport | None = None,
        evidence_ref: str | None = None,
    ) -> AttemptRecord:
        self._policy.require_operator(operator)
        if resolution not in {
            "no_side_effect", "executed", "still_unknown",
        }:
            raise ReconciliationRequiredError("unknown resolution is not supported")
        reason = _text_reason(reason)
        now = self._iso_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT a.*, p.task_json FROM attempts a "
                "JOIN proposals p ON p.proposal_digest = a.proposal_digest "
                "WHERE a.attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise AttemptStateError("attempt is not registered")
            if row["state"] not in RECONCILABLE_STATES:
                raise AttemptStateError(
                    f"attempt state {row['state']} does not require reconciliation"
                )

            cancelled = row["cancel_requested_at"] is not None
            if resolution == "still_unknown":
                next_state = row["state"]
            elif resolution == "no_side_effect":
                if not evidence_ref:
                    raise ReconciliationRequiredError(
                        "no_side_effect requires an evidence reference"
                    )
                if row["report_json"] is not None:
                    raise ReconciliationRequiredError(
                        "a stored worker report contradicts a no-side-effect claim"
                    )
                next_state = ATTEMPT_CANCELLED if cancelled else (
                    ATTEMPT_FAILED_NO_SIDE_EFFECT
                )
            else:  # executed
                if report is None:
                    raise ReconciliationRequiredError(
                        "executed reconciliation requires a worker report"
                    )
                task = _task_from_payload(json.loads(row["task_json"]))
                self._validate_report_against_attempt(report, task, row)
                if row["report_json"] is not None and (
                    row["report_json"] != canonical_json(report.to_dict())
                ):
                    raise ReportConflictError(
                        "reconciliation report contradicts stored report"
                    )
                connection.execute(
                    "UPDATE attempts SET report_json = COALESCE(report_json, ?), "
                    "provider_run_id = COALESCE(provider_run_id, ?) "
                    "WHERE attempt_id = ?",
                    (canonical_json(report.to_dict()), report.provider_run_id,
                     attempt_id),
                )
                next_state = (
                    ATTEMPT_CANCELLED_UNCERTAIN if cancelled else ATTEMPT_REPORTED
                )

            connection.execute(
                "UPDATE attempts SET state = ?, updated_at = ?, "
                "last_transition_reason = ? WHERE attempt_id = ?",
                (next_state, now, f"reconciled_{resolution}", attempt_id),
            )
            self._event(
                connection, "unknown_outcome_reconciled", now,
                proposal_digest=row["proposal_digest"], attempt_id=attempt_id,
                detail={
                    "operator": operator.principal_id,
                    "resolution": resolution,
                    "evidence_ref": evidence_ref,
                    "reason": reason,
                    "final_state": next_state,
                },
            )
            return _attempt_from_row(connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,),
            ).fetchone())

    def sweep(self, *, stale_after_seconds: int | None = None) -> RecoveryReport:
        """Expire unconsumed grants and classify stale in-flight attempts.

        A consumed grant whose dispatch never reported back becomes ``unknown``;
        it is never silently returned to ``granted``.
        """

        stale = (
            self._policy.stale_attempt_seconds
            if stale_after_seconds is None
            else stale_after_seconds
        )
        if not isinstance(stale, int) or isinstance(stale, bool) or not (
            1 <= stale <= 604_800
        ):
            raise AuthorityConfigurationError("invalid stale_after_seconds")
        now_dt = self._now()
        now = _timestamp(now_dt, "now")
        cutoff = _timestamp(now_dt - timedelta(seconds=stale), "cutoff")
        expired_grants: list[str] = []
        expired_attempts: list[str] = []
        unknown_attempts: list[str] = []

        with self._transaction() as connection:
            grant_rows = connection.execute(
                "SELECT g.*, a.state AS attempt_state FROM grants g "
                "JOIN attempts a ON a.attempt_id = g.attempt_id "
                "WHERE g.consumed_at IS NULL AND g.revoked_at IS NULL "
                "AND g.expires_at <= ?",
                (now,),
            ).fetchall()
            for row in grant_rows:
                connection.execute(
                    "UPDATE grants SET revoked_at = ?, "
                    "revocation_reason = 'expired' WHERE grant_id = ?",
                    (now, row["grant_id"]),
                )
                expired_grants.append(row["grant_id"])
                if row["attempt_state"] == ATTEMPT_GRANTED:
                    connection.execute(
                        "UPDATE attempts SET state = ?, updated_at = ?, "
                        "last_transition_reason = 'grant_expired' "
                        "WHERE attempt_id = ?",
                        (ATTEMPT_EXPIRED, now, row["attempt_id"]),
                    )
                    expired_attempts.append(row["attempt_id"])
                self._event(
                    connection, "grant_expired", now,
                    proposal_digest=row["proposal_digest"],
                    attempt_id=row["attempt_id"],
                    detail={"grant_id": row["grant_id"]},
                )

            stale_rows = connection.execute(
                "SELECT * FROM attempts WHERE state IN (?, ?) AND updated_at <= ?",
                (ATTEMPT_DISPATCHING, ATTEMPT_DISPATCHED, cutoff),
            ).fetchall()
            for row in stale_rows:
                connection.execute(
                    "UPDATE attempts SET state = ?, updated_at = ?, "
                    "last_transition_reason = 'stale_attempt_unknown' "
                    "WHERE attempt_id = ?",
                    (ATTEMPT_UNKNOWN, now, row["attempt_id"]),
                )
                unknown_attempts.append(row["attempt_id"])
                self._event(
                    connection, "attempt_became_unknown", now,
                    proposal_digest=row["proposal_digest"],
                    attempt_id=row["attempt_id"],
                    detail={"previous_state": row["state"]},
                )

        return RecoveryReport(
            swept_at=now,
            expired_grants=tuple(expired_grants),
            expired_attempts=tuple(expired_attempts),
            unknown_attempts=tuple(unknown_attempts),
        )

    def recover_after_restart(
        self, *, stale_after_seconds: int | None = None,
    ) -> RecoveryReport:
        """Call at startup.  It never re-grants or blindly retries a task."""

        return self.sweep(stale_after_seconds=stale_after_seconds)


def _text_reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
        raise AuthorityConfigurationError(
            "reason must be a non-empty string of at most 2000 characters"
        )
    if "\x00" in value:
        raise AuthorityConfigurationError("reason must not contain NUL")
    return value


# ---------------------------------------------------------------------------
# Public dictionary adapters for the authenticated service boundary
# ---------------------------------------------------------------------------


def task_spec_from_dict(payload: dict[str, Any]) -> TaskSpec:
    """Reconstruct a canonical task from client JSON; never trusts a digest."""

    if not isinstance(payload, dict):
        raise ContractValidationError("task payload must be an object")
    return _task_from_payload(payload)


def execution_grant_from_dict(payload: dict[str, Any]) -> ExecutionGrant:
    if not isinstance(payload, dict):
        raise ContractValidationError("grant payload must be an object")
    return _grant_from_payload(payload)


def execution_report_from_dict(payload: dict[str, Any]) -> ExecutionReport:
    if not isinstance(payload, dict):
        raise ContractValidationError("report payload must be an object")
    return _report_from_payload(payload)


def verification_record_from_dict(payload: dict[str, Any]) -> VerificationRecord:
    if not isinstance(payload, dict):
        raise ContractValidationError("verification payload must be an object")
    return _verification_from_payload(payload)
