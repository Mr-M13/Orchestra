# ADR-009: Authenticated authority-to-disposable-adapter integration

- **Status:** Proposed, inactive prototype
- **Date:** 2026-10-21
- **Related:** [ADR-007](ADR-007-execution-authority-prototype.md),
  [ADR-008](ADR-008-authority-security-boundary.md),
  [coding-worker adapter](../CODING-WORKER-ADAPTER.md)

## Decision

The disposable coding-worker adapter uses the authenticated authority Unix
socket to consume, rather than merely validate, an execution grant before it
starts a process. The authority remains the sole owner of approvals, grant
issuance and atomic single-use state. The adapter receives no authority database
access and makes no approval decision.

For each attempt the adapter first persists an owner-only execution intent with
the exact proposal, grant and a cryptographically random consume request ID. It
then asks the authority to atomically validate and consume the exact TaskSpec,
proposal, attempt, adapter and runtime binding. A complete consume receipt is
durably written before the adapter records its launch intent and starts the
fixed bundled disposable harness.

The consume request ID belongs to one local intent, not to the grant globally.
This prevents two independent adapters from treating the same idempotent cached
response as two launch permissions. The authority's transaction makes one
concurrent consumption succeed and rejects all other consume request IDs. An
attempt-scoped OS file lock additionally serializes adapter processes that share
the same durable state root.

## Recovery

- A crash before consumption is recovered by querying the durable RPC request.
  Only a definitive `not_found` permits submission of that same request ID.
- A recorded authority success restores the exact receipt without consuming
  again.
- A crash after the receipt is persisted but before launch may safely launch
  from the same intent.
- A crash after the durable `launching` transition is an unknown outcome. The
  adapter never launches again automatically.
- A running process is adopted only when its PID and Linux process-start value
  match. A missing or changed process becomes unknown.
- A terminal worker result is bounded and hashed again by the adapter. It is
  still an untrusted report until an independent verifier records a decision.

## Authentication and roles

The authority server derives the caller UID from Unix peer credentials. The
coordinator role may propose, request and consume a grant. A separately
configured worker role records dispatch and the untrusted execution report. A
separate verifier role records verification. Caller-supplied identities are not
accepted. The disposable Linux boundary demonstration exercises these roles
under separate real UIDs.

## Consequences and limits

This is intentionally not a production dispatch path. It is not imported by
OMCP, installs no service, accesses no credential and launches no provider. The
adapter and coordinator execute under one disposable coordinator principal; a
future production runtime must define a separately isolated worker boundary.
The adapter-owned files are durable enough for crash tests but are not a
tamper-resistant security boundary against their owning coordinator.

The launch syscall and the subsequent durable PID record cannot be one atomic
operation. Persisting `launching` first and refusing automatic recovery is the
fail-closed choice: it may require operator reconciliation, but it cannot cause
an uncertain task to run twice.

## Validation

Unit and integration tests cover concurrent consumption, replay, exact binding,
authority unavailability and unauthenticated peers, crashes on each side of
consumption and launch, process-group cancellation, restart recovery, bounded
artifact hashing and independent verification. The disposable Linux boundary
test additionally runs the authority, coordinator, approver, worker and
verifier as separate operating-system UIDs over the real Unix socket.
