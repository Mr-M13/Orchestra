# ADR-007: Disposable independent execution-authority prototype

- **Status:** Prototype only — not activated, not deployed, not production.
- **Base:** `main` containing ADR-006 and PR #43.
- **Implementation:** `orchestra/authority.py`, `orchestra/authority_demo.py`,
  `tests/test_execution_authority.py`.
- **Related:** [ADR-006](ADR-006-provider-independent-agent-architecture.md).

## 1. Context

ADR-006 defined provider-independent contracts for proposals, grants, reports
and independent verification, but deliberately implemented no enforcement. A
`TaskSpec` digest and an unsigned `ExecutionGrant` were descriptive: possession
of a grant proved nothing, replay was not prevented, and no durable state
existed.

This ADR records a **small, disposable prototype** that enforces the contract
boundary independently of a coordinator and worker. It is a Python library, not
a service. It is not imported by OMCP, systemd, MCP Tunnel, Nextcloud, Docker,
PixelBox or any live dispatch path. No provider credentials, signing keys or
external execution are used.

The prototype exists to test one question: can an authority enforce approval,
single-use grant audience, untrusted reports, independent verification and
safe recovery without relying on the coordinator or worker to behave?

## 2. Scope and non-goals

### In scope

- Durable SQLite state for proposals, approvals, attempts, grants, grant
  consumption/expiry/revocation, reports, verification records, cancellations
  and audit events.
- Exact canonical `TaskSpec` digest binding.
- Exact agent/model/harness/adapter/runtime audience binding.
- Single-use, expiring, nonce-bearing grants with atomic consumption.
- Simulated human approval and bounded routine standing authorisation.
- Untrusted worker reports that never verify themselves.
- Separate trusted-verifier records and fail-closed verification outcomes.
- Idempotent requests, cancellation semantics, stale-attempt detection,
  crash/restart recovery and unknown-outcome reconciliation.
- A fake-worker demonstration in a temporary directory.

### Explicit non-goals

- A production authority daemon, network service or separate OS process.
- Production identity, authentication, signatures or key management.
- Credential brokering, vault integration or secret isolation.
- Real provider dispatch, real worker execution or live OMCP integration.
- Changes to OMCP dispatch, approvals, systemd, MCP Tunnel, Nextcloud, Docker,
  PixelBox, GitHub Actions workflows or the old DeepSeek worker.
- Enabling or re-enabling any coding worker.
- Phase 3 adapter migration.

## 3. Architecture

`PrototypeExecutionAuthority` is a library that owns one SQLite database and one
immutable, owner-configured `AuthorityPolicy`.

```text
TaskSpec + canonical digest
  -> proposal record (idempotent by digest; request id conflict detection)
  -> approval (simulated human decision or exact standing rule)
  -> attempt + single-use ExecutionGrant (audience-bound)
  -> atomic grant consumption
  -> dispatch receipt (provider run identity)
  -> untrusted ExecutionReport
  -> independent VerificationRecord
  -> verified | rejected
```

### 3.1 Durable tables

| Table | Purpose |
| --- | --- |
| `proposals` | Canonical task bytes, digest, task/request identity, proposer |
| `approvals` | Human or standing decisions bound to the exact digest |
| `attempts` | Attempt state machine, idempotency key, audience, report, cancellation |
| `grants` | Issued grant bytes, nonce, expiry, consumption and revocation |
| `verifications` | Every independent verification record, including contradictions |
| `cancellations` | Idempotent cancellation requests and applied state |
| `events` | Append-only audit trail |
| `meta` | Prototype marker, schema version, authority id and policy digest |

Every mutation uses `BEGIN IMMEDIATE`, `PRAGMA synchronous=FULL`,
`PRAGMA foreign_keys=ON` and WAL journaling. A crash therefore leaves either the
whole transition or none of it. The `events` table has `BEFORE UPDATE` and
`BEFORE DELETE` triggers that abort mutation.

### 3.2 Policy binding

`AuthorityPolicy` is frozen. It contains:

- the trusted simulated human, verifier and operator identities;
- the exact allowed execution audiences;
- bounded standing authorisations;
- grant, approval and stale-attempt windows.

There is no method to mutate policy. On opening an existing database the
authority compares the stored `authority_id` and `policy_digest`; a widened or
otherwise changed policy is rejected. This is a prototype-level defence, not a
substitute for owner-only configuration and filesystem permissions.

### 3.3 Approval

- A proposal is never an approval.
- Only a principal asserting the `human` role and matching one of the trusted
  human identities can record an approval or denial.
- The coordinator can propose and request execution, but cannot decide approval.
- Contradictory current human decisions are rejected.
- Standing authorisation is considered only when all of the following are exact:
  repository, agent, provider, model, harness, adapter, runtime, routine
  approval class, routine-development consequence class, operation set, write
  roots, network-denied policy and resource limits.
- Standing rules cannot be configured with sensitive operations
  (`push_branch`, `open_pull_request`, `merge`, `deploy`, `access_network`,
  `use_credentials`). Sensitive and unclassified consequences require an
  explicit human decision.

### 3.4 Grant enforcement

A grant binds the proposal digest, task/request identity, attempt, adapter,
runtime, repository, base commit, agent, model provider, model, harness, file
scope, operations, nonce, issue time, expiry and issuer.

Consumption and validation require:

1. the grant id exists in durable state;
2. the presented canonical grant bytes exactly equal the issued bytes;
3. the nonce and attempt binding match;
4. the grant is unexpired, unconsumed and unrevoked;
5. the current task canonical bytes and digest match the durable proposal;
6. the exact configured audience matches;
7. contract-level binding validation passes.

Consumption updates `consumed_at` and the attempt state inside one transaction.
The update includes `consumed_at IS NULL`, and a lost race is rejected. The same
transaction is committed before any dispatch, so a crash cannot make a consumed
grant reusable.

### 3.5 Reports and verification

A worker report is stored as `untrusted_worker_report`. Recording it transitions
the attempt only to `reported` (or leaves a cancellation-uncertain attempt
uncertain). It cannot set `verified`.

Only a principal asserting the `verifier` role, matching a trusted verifier
identity, and matching the record's `verifier_id` may submit a verification
record. The record must bind to the stored report, task, proposal and attempt.
Passing records verify; failing records reject. Contradictory records fail
closed:

- pass followed by failure downgrades the attempt to `rejected`;
- failure followed by pass is stored but cannot upgrade the attempt;
- verification against a cancelled or unknown attempt is recorded but not
  applied.

### 3.6 Cancellation and recovery

- Cancellation before consumption revokes the grant and moves the attempt to
  `cancelled`; only then may a new attempt be requested.
- Cancellation after consumption moves the attempt to `cancelled_uncertain`.
  Late reports are retained as evidence but cannot be verified.
- Cancellation of an unknown attempt leaves it `unknown` with a cancellation
  request recorded.
- Cancellation after a terminal state is idempotently reported as
  `already_terminal`.
- `sweep`/`recover_after_restart` expires unconsumed grants and classifies stale
  `dispatching`/`dispatched` attempts as `unknown`. It never reissues a grant.
- Unknown or cancelled-uncertain attempts block new attempts for the same task
  and request until an operator explicitly reconciles them.
- Reconciliation supports `no_side_effect` (requires evidence and is refused if
  a worker report already exists), `executed` (requires a matching report) and
  `still_unknown`. Reconciliation does not automatically retry.

## 4. Demonstrated security properties

The implementation and tests demonstrate the following **within an in-process
prototype**:

| Property | Test evidence |
| --- | --- |
| Modified TaskSpec after approval is rejected | `test_modified_task_after_approval_is_rejected`, `test_approval_does_not_transfer_to_a_changed_candidate` |
| Expired grant is rejected and swept | `test_expired_grant_is_rejected_and_swept` |
| Replayed grant is rejected | `test_replayed_grant_is_rejected` |
| Duplicate and conflicting requests are rejected or idempotent | `test_duplicate_and_conflicting_requests` |
| Incorrect adapter/runtime/model audience is rejected | `test_wrong_adapter_runtime_and_agent_audience_are_rejected`, `test_report_and_dispatch_audience_must_match` |
| Unauthorised approval is rejected | `test_coordinator_cannot_approve_or_self_authorise` |
| Coordinator impersonation is rejected | `test_coordinator_impersonation_is_rejected` |
| Policy widening across restart is rejected | `test_policy_cannot_be_widened_by_reopening_the_database` |
| Worker success is not verification | `test_worker_success_is_never_automatic_verification` |
| A report cannot bypass provider-run dispatch identity | `test_report_without_dispatch_receipt_is_rejected` |
| False worker success is rejected by independent verification | `test_false_worker_success_is_rejected_by_independent_verification` |
| Contradictory verification evidence fails closed | `test_contradictory_verification_evidence_fails_closed` |
| Interrupted grant consumption rolls back atomically | `test_interrupted_consumption_transaction_rolls_back` |
| Concurrent grant consumption has exactly one winner | `test_atomic_single_use_under_concurrent_consumers` |
| Crash/restart preserves durable state and detects stale attempts | `test_full_lifecycle_and_durable_state`, `test_crash_restart_detects_stale_attempt_as_unknown` |
| Cancellation races fail closed | `test_cancellation_before_consumption_revokes_grant_safely`, `test_cancellation_after_consumption_is_uncertain_and_unverifiable`, `test_cancellation_race_with_report_is_fail_closed` |
| Unknown execution outcome blocks blind retry | `test_unknown_outcome_blocks_blind_retry_and_requires_reconciliation` |
| Unknown outcome can be reconciled without guessing | `test_unknown_outcome_can_be_reconciled_as_executed_with_report`, `test_still_unknown_reconciliation_does_not_unlock_retry` |
| Audit events are append-only | `test_audit_events_are_append_only` |
| Full disposable lifecycle works with a fake worker | `test_demo_runs_full_lifecycle_in_a_temporary_directory` |

## 5. Security properties NOT demonstrated

These remain requirements for a real authority and are deliberately outside
this prototype:

1. **Separate process and service identity.** The authority is a library in the
   caller's process. A coordinator with code execution can call private methods,
   edit the SQLite file or patch the module. A real authority must run under a
   separate OS identity with filesystem permissions that exclude the
   coordinator.
2. **Authenticated principals and IPC.** `Principal(kind=...)` is asserted by
   the caller, not authenticated. A production design needs authenticated local
   IPC, owner-only sockets, process credentials or equivalent. The current role
   checks demonstrate intended policy, not identity proof.
3. **Grant authenticity and signing.** Grants remain unsigned contract-only
   records. Database membership prevents casual forgery, but a caller that can
   write the database can mint state. Production grants need signing or an
   equally strong authority-owned capability.
4. **Credential boundary.** No credentials, credential gateway or broker are
   implemented. Credential isolation, provider tokens and secret handling must
   remain outside the authority and be enforced by the runtime.
5. **Real execution isolation.** The fake worker performs a local file write.
   Filesystem sandboxing, network egress control, process isolation, resource
   limits and provider-run identity binding are not tested.
6. **Evidence validation.** Verification records contain evidence references
   and hashes. The authority checks binding, identity and pass/fail semantics;
   it does not independently fetch or validate external evidence. The demo
   verifier recomputes a local hash, but a real verifier must be independently
   operated.
7. **Trusted time and nonce quality.** Expiry uses the process clock. Clock
   tampering, long-lived copies of the database and nonce unpredictability
   beyond `secrets.token_urlsafe` are not addressed.
8. **Availability and denial of service.** SQLite locking, disk exhaustion,
   process crashes during commit and resource exhaustion are not modelled
   beyond local transaction rollback and restart tests.
9. **Semantic instruction attacks.** A task whose instructions ask for
   sensitive work is still constrained by its declared operations and scope, but
   the prototype does not interpret natural-language intent. Adapters and
   verifiers must enforce scope independently.
10. **Live migration.** Nothing in OMCP, approvals, systemd, MCP Tunnel,
    Nextcloud, Docker, PixelBox or GitHub Actions has been changed or tested with
    this authority. There is no production path or rollback rehearsal.

## 6. Rollback

This change is strictly additive and disconnected from live execution:

- new files only under `orchestra/`, `tests/` and `docs/decisions/`;
- no changes to OMCP modules, deployment units, workflows, credentials or
  provider configuration;
- reverting the branch removes the prototype entirely;
- no database migration, service, socket or credential was created in the live
  environment;
- the demo uses a temporary directory and deletes it unless an explicit
  disposable path is supplied.

## 7. Running the prototype

```bash
python3 -m unittest tests.test_execution_authority -v
python3 -m orchestra.authority_demo
```

The demo prints the proposal digest, approval source, grant, consumption time,
attempt identity, the intermediate `reported` state, the final `verified` state
and the audit event sequence. It requires no network or provider credentials.

## 8. Verdict

**PASS for the disposable prototype only.** The stated in-process security
properties are covered by adversarial tests. The prototype is **BLOCKED** for
production activation until the separate-process, authenticated-identity,
credential-boundary, signing, evidence-validation and live rollback requirements
above are implemented and independently reviewed.
